// -----------------------------------------------------------------------------
// tt_um_pwmGen.sv - Generador PWM con duty cycles fijos seleccionados por entrada
// Interfaz estandar de Tiny Tapeout
//
// ui_in[7:0]  : seleccion de duty (prioridad: ui_in[0] > ui_in[1] > ... > ui_in[7])
//                 in0=15%  in1=25%  in2=35%  in3=50%
//                 in4=65%  in5=75%  in6=85%  in7=95%
//               Ninguna entrada activa -> salida PWM en 0
//
// uio[7:0]    : bidireccionales (uio_oe = 8'b1111_1000)
//                 uio[0] = EN    (entrada)
//                 uio[1] = SEL0  (entrada)  -> divisor de reloj
//                 uio[2] = SEL1  (entrada)
//                 uio[3] = PWM
//                 uio[4] = PWM negado
//                 uio[5] = fin de periodo
//                 uio[6] = MSB del contador
//                 uio[7] = tick                                  (salidas)
//
// uo_out[7:0] : salidas
//                 uo_out[0] = PWM
//                 uo_out[1] = PWM negado
//                 uo_out[2] = fin de periodo (ultimo ciclo del periodo PWM)
//                 uo_out[3] = tick del prescaler
//                 uo_out[4] = EN (sincronizado)
//                 uo_out[5] = MSB del contador
//                 uo_out[6] = any_sel   (hay alguna entrada de duty activa)
//                 uo_out[7] = multi_sel (mas de una entrada de duty activa)
//
// Divisor: SEL1:SEL0 = 00:/1  01:/8  10:/64  11:/512
// f_pwm = f_clk / (divisor * 2^WIDTH)
//
// Comportamiento:
//  - Mientras EN = 0 el contador y el prescaler se mantienen en 0.
//  - El duty (umbral) y el divisor se capturan solo con EN = 0 o al final de
//    cada periodo, asi un cambio de ui_in o SEL en vuelo se aplica en el
//    siguiente periodo completo (sin periodos recortados).
//  - Todas las salidas estan registradas y alineadas en el mismo ciclo.
// -----------------------------------------------------------------------------
`default_nettype none

module tt_um_pwmGen #(
    parameter WIDTH = 8,                 // bits del contador
    // Duty cycles en porcentaje
    parameter D0 = 15,
    parameter D1 = 25,
    parameter D2 = 35,
    parameter D3 = 50,
    parameter D4 = 65,
    parameter D5 = 75,
    parameter D6 = 85,
    parameter D7 = 95
)(
    input  wire [7:0] ui_in,    // Entradas dedicadas
    output wire [7:0] uo_out,   // Salidas dedicadas
    input  wire [7:0] uio_in,   // IOs: camino de entrada
    output wire [7:0] uio_out,  // IOs: camino de salida
    output wire [7:0] uio_oe,   // IOs: habilitacion de salida (1 = salida)
    input  wire       ena,      // Siempre 1 cuando el diseno esta alimentado
    input  wire       clk,
    input  wire       rst_n     // Reset activo en bajo
);

    // -------------------------------------------------------------------------
    // Umbrales del comparador para el duty cycle (T = D * 2^WIDTH / 100)
    // -------------------------------------------------------------------------
    localparam [WIDTH-1:0] T0 = (D0 * (2**WIDTH)) / 100;
    localparam [WIDTH-1:0] T1 = (D1 * (2**WIDTH)) / 100;
    localparam [WIDTH-1:0] T2 = (D2 * (2**WIDTH)) / 100;
    localparam [WIDTH-1:0] T3 = (D3 * (2**WIDTH)) / 100;
    localparam [WIDTH-1:0] T4 = (D4 * (2**WIDTH)) / 100;
    localparam [WIDTH-1:0] T5 = (D5 * (2**WIDTH)) / 100;
    localparam [WIDTH-1:0] T6 = (D6 * (2**WIDTH)) / 100;
    localparam [WIDTH-1:0] T7 = (D7 * (2**WIDTH)) / 100;

    // -------------------------------------------------------------------------
    // Direccion de los IOs bidireccionales: [2:0] entradas, [7:3] salidas
    // -------------------------------------------------------------------------
    assign uio_oe = 8'b1111_1000;

    // -------------------------------------------------------------------------
    // Sincronizadores de 2 FF para entradas y reset
    // -------------------------------------------------------------------------
    logic [1:0] rst_sync;
    logic [7:0] in_s1, in_s2;
    logic [2:0] io_s1, io_s2;            // {SEL1, SEL0, EN}

    logic rst_ok;                        // 0 = en reset
    assign rst_ok = rst_sync[1];

    always_ff @(posedge clk) begin
        rst_sync <= {rst_sync[0], rst_n};
        in_s1    <= ui_in;
        in_s2    <= in_s1;
        io_s1    <= uio_in[2:0];
        io_s2    <= io_s1;
    end

    logic       en;
    logic [1:0] sel;
    assign en  = io_s2[0];
    assign sel = io_s2[2:1];

    // -------------------------------------------------------------------------
    // Seleccion combinacional del umbral (prioridad: menor indice)
    // -------------------------------------------------------------------------
    logic [WIDTH-1:0] thr_c;
    logic             any_sel_c;

    always_comb begin
        any_sel_c = 1'b1;
        if      (in_s2[0]) thr_c = T0;
        else if (in_s2[1]) thr_c = T1;
        else if (in_s2[2]) thr_c = T2;
        else if (in_s2[3]) thr_c = T3;
        else if (in_s2[4]) thr_c = T4;
        else if (in_s2[5]) thr_c = T5;
        else if (in_s2[6]) thr_c = T6;
        else if (in_s2[7]) thr_c = T7;
        else begin
            thr_c     = {WIDTH{1'b0}};
            any_sel_c = 1'b0;
        end
    end

    // Mas de una entrada activa (bandera de aviso)
    logic multi_sel_c;
    assign multi_sel_c = |(in_s2 & (in_s2 - 8'd1));

    // -------------------------------------------------------------------------
    // Captura de duty y divisor: solo con EN = 0 o al final del periodo
    // -------------------------------------------------------------------------
    logic             period_end;        // ultimo ciclo del periodo (definido abajo)
    logic             load;
    logic [WIDTH-1:0] thr;
    logic             any_sel;
    logic [1:0]       sel_l;

    assign load = ~en | period_end;

    // Durante el reset tambien se captura, asi tras un reset con EN = 1 el
    // primer periodo ya usa el duty seleccionado (y no queda en 0).
    always_ff @(posedge clk) begin
        if (!rst_ok || load) begin
            thr     <= thr_c;
            any_sel <= any_sel_c;
            sel_l   <= sel;
        end
    end

    // -------------------------------------------------------------------------
    // Prescaler (se mantiene en 0 mientras EN = 0 para tener un primer paso exacto)
    // -------------------------------------------------------------------------
    logic [8:0] pre_cnt;
    logic       tick;

    always_ff @(posedge clk) begin
        if (!rst_ok || !en) pre_cnt <= 9'd0;
        else                pre_cnt <= pre_cnt + 9'd1;
    end

    always_comb begin
        case (sel_l)
            2'b00:   tick = 1'b1;
            2'b01:   tick = &pre_cnt[2:0];
            2'b10:   tick = &pre_cnt[5:0];
            default: tick = &pre_cnt[8:0];
        endcase
    end

    // -------------------------------------------------------------------------
    // Contador del PWM
    // -------------------------------------------------------------------------
    logic [WIDTH-1:0] cnt;

    assign period_end = tick & en & (&cnt);

    always_ff @(posedge clk) begin
        if (!rst_ok)    cnt <= {WIDTH{1'b0}};
        else if (!en)   cnt <= {WIDTH{1'b0}};
        else if (tick)  cnt <= cnt + 1'b1;
    end

    // -------------------------------------------------------------------------
    // Comparador y salidas registradas (todas con 1 ciclo de latencia, alineadas)
    // -------------------------------------------------------------------------
    logic pwm_r, period_end_r, tick_r, en_r, msb_r, any_sel_r, multi_sel_r;

    always_ff @(posedge clk) begin
        if (!rst_ok) begin
            pwm_r        <= 1'b0;
            period_end_r <= 1'b0;
            tick_r       <= 1'b0;
            en_r         <= 1'b0;
            msb_r        <= 1'b0;
            any_sel_r    <= 1'b0;
            multi_sel_r  <= 1'b0;
        end else begin
            pwm_r        <= en & any_sel & (cnt < thr);
            period_end_r <= period_end;
            tick_r       <= tick & en;
            en_r         <= en;
            msb_r        <= cnt[WIDTH-1];
            any_sel_r    <= any_sel_c;
            multi_sel_r  <= multi_sel_c;
        end
    end

    // -------------------------------------------------------------------------
    // Salidas del modulo
    // -------------------------------------------------------------------------
    assign uo_out[0] = pwm_r;
    assign uo_out[1] = ~pwm_r;
    assign uo_out[2] = period_end_r;
    assign uo_out[3] = tick_r;
    assign uo_out[4] = en_r;
    assign uo_out[5] = msb_r;
    assign uo_out[6] = any_sel_r;
    assign uo_out[7] = multi_sel_r;

    // IOs bidireccionales: [2:0] son entradas (salida en 0), [7:3] son salidas
    assign uio_out[0] = 1'b0;
    assign uio_out[1] = 1'b0;
    assign uio_out[2] = 1'b0;
    assign uio_out[3] = pwm_r;
    assign uio_out[4] = ~pwm_r;
    assign uio_out[5] = period_end_r;
    assign uio_out[6] = msb_r;
    assign uio_out[7] = tick_r;

    // Evita warnings por entradas no usadas
    wire _unused = &{ena, uio_in[7:3], 1'b0};

endmodule
