# SPDX-License-Identifier: Apache-2.0
# Testbench cocotb para tt_um_pwmGen

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

WIDTH = 8
PERIOD = 2**WIDTH                                   # 256 cuentas
# Umbrales con la misma formula que el RTL (division entera = truncamiento)
DUTY_PCT = [15, 25, 35, 50, 65, 75, 85, 95]
THR = [(d * PERIOD) // 100 for d in DUTY_PCT]       # 38,64,89,128,166,192,217,243
DIV = {0: 1, 1: 8, 2: 64, 3: 512}

# Bits de uo_out
PWM, PWM_N, PEND, TICK, EN_O, MSB, ANY, MULTI = range(8)
# Bits de uio_in
EN_BIT, SEL0_BIT, SEL1_BIT = 0, 1, 2


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def bit(sig, n):
    """Lee el bit n de una senal (LogicArray) como 0/1."""
    return (int(sig.value) >> n) & 1


def drive(dut, duty_in=None, en=None, sel=None):
    """Aplica ui_in y/o uio_in[2:0] (EN, SEL1:SEL0). None = no cambiar."""
    if duty_in is not None:
        dut.ui_in.value = duty_in
    if en is not None or sel is not None:
        cur = int(dut.uio_in.value)
        e = (cur & 1) if en is None else en
        s = ((cur >> 1) & 3) if sel is None else sel
        dut.uio_in.value = (s << 1) | e


async def start(dut, period_ns=40):
    cocotb.start_soon(Clock(dut.clk, period_ns, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)


async def sample(dut, n):
    """Toma n muestras consecutivas de uo_out (una por ciclo)."""
    out = []
    for _ in range(n):
        await RisingEdge(dut.clk)
        out.append(int(dut.uo_out.value))
    return out


async def wait_period_end(dut, max_cycles):
    """Avanza hasta ver el pulso de fin de periodo (uo_out[2]).
    Al volver, la siguiente muestra es el primer ciclo de un periodo nuevo."""
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        if bit(dut.uo_out, PEND):
            return
    assert False, f"No aparecio period_end en {max_cycles} ciclos"


async def measure_period(dut, div):
    """Mide un periodo completo (despues de wait_period_end).
    Devuelve (ciclos_en_alto, ciclos_totales, muestras)."""
    n = PERIOD * div
    s = await sample(dut, n)
    high = sum((v >> PWM) & 1 for v in s)
    return high, n, s


def check_shape(s, expected_high):
    """El PWM debe ser un unico pulso alto al inicio y luego bajo."""
    bits = [(v >> PWM) & 1 for v in s]
    assert bits[:expected_high] == [1] * expected_high, "El pulso alto no es contiguo desde el inicio"
    assert bits[expected_high:] == [0] * (len(bits) - expected_high), "Hay PWM alto despues del umbral"


# ----------------------------------------------------------------------------
# Pruebas
# ----------------------------------------------------------------------------
@cocotb.test()
async def test_reset_and_idle(dut):
    """Tras reset con EN=0: PWM apagado, PWM_N alto, IOs con la direccion correcta."""
    dut._log.info("Reset y estado inicial")
    await start(dut)
    for _ in range(20):
        await RisingEdge(dut.clk)
        assert bit(dut.uo_out, PWM) == 0
        assert bit(dut.uo_out, PWM_N) == 1
        assert bit(dut.uo_out, PEND) == 0
        assert bit(dut.uo_out, TICK) == 0
        assert bit(dut.uo_out, EN_O) == 0
    assert int(dut.uio_oe.value) == 0b11111000, "uio_oe debe ser 8'b1111_1000"

    # Aun con una entrada de duty activa, sin EN el PWM sigue apagado
    drive(dut, duty_in=0x08)
    s = await sample(dut, 600)
    assert all(((v >> PWM) & 1) == 0 for v in s), "PWM activo con EN=0"
    assert all(((v >> ANY) & 1) == 1 for v in s[10:]), "any_sel deberia ser 1"


@cocotb.test()
async def test_duty_each_input(dut):
    """Cada entrada ui_in[i] genera el duty esperado (divisor /1), 2 periodos seguidos."""
    await start(dut)
    for i in range(8):
        dut._log.info(f"ui_in[{i}] -> umbral {THR[i]} ({THR[i]/PERIOD*100:.2f}%)")
        drive(dut, duty_in=1 << i, en=0, sel=0)
        await ClockCycles(dut.clk, 6)
        drive(dut, en=1)
        await wait_period_end(dut, 3 * PERIOD)
        for k in range(2):
            high, n, s = await measure_period(dut, 1)
            assert high == THR[i], f"ui_in[{i}] periodo {k}: alto={high}, esperado={THR[i]}"
            check_shape(s, THR[i])
            assert (s[-1] >> PEND) & 1 == 1, "period_end debe caer en el ultimo ciclo del periodo"
            assert sum((v >> PEND) & 1 for v in s) == 1, "period_end debe ser un solo pulso por periodo"
            assert sum((v >> MULTI) & 1 for v in s) == 0, "multi_sel no deberia activarse"
        drive(dut, en=0)
        await ClockCycles(dut.clk, 6)


@cocotb.test()
async def test_priority_and_flags(dut):
    """Prioridad (menor indice gana), any_sel y multi_sel."""
    await start(dut)
    drive(dut, en=1, sel=0)

    casos = [
        # (ui_in,        umbral esperado, any, multi)
        (0b1010_0100,    THR[2],          1,   1),   # bits 2,5,7 -> gana 2
        (0b1000_0001,    THR[0],          1,   1),   # bits 0,7   -> gana 0
        (0b1111_1111,    THR[0],          1,   1),   # todas      -> gana 0
        (0b1100_0000,    THR[6],          1,   1),   # bits 6,7   -> gana 6
        (0b0001_0000,    THR[4],          1,   0),   # solo bit 4
    ]
    for ui, thr_esp, any_esp, multi_esp in casos:
        dut._log.info(f"ui_in={ui:08b} -> umbral esperado {thr_esp}")
        drive(dut, duty_in=ui)
        await ClockCycles(dut.clk, 5)
        # el nuevo duty se aplica al siguiente periodo completo
        await wait_period_end(dut, 2 * PERIOD)
        high, n, s = await measure_period(dut, 1)
        assert high == thr_esp, f"ui_in={ui:08b}: alto={high}, esperado={thr_esp}"
        assert all(((v >> ANY) & 1) == any_esp for v in s), "any_sel incorrecto"
        assert all(((v >> MULTI) & 1) == multi_esp for v in s), "multi_sel incorrecto"

    # Ninguna entrada activa -> PWM en 0 y any_sel en 0
    drive(dut, duty_in=0)
    await ClockCycles(dut.clk, 5)
    await wait_period_end(dut, 2 * PERIOD)
    high, n, s = await measure_period(dut, 1)
    assert high == 0, "PWM debe estar en 0 sin entradas de duty"
    assert all(((v >> ANY) & 1) == 0 for v in s), "any_sel debe ser 0"
    assert all(((v >> MULTI) & 1) == 0 for v in s), "multi_sel debe ser 0"


@cocotb.test()
async def test_clock_dividers(dut):
    """Periodo y duty para /1, /8, /64 y /512 (duty 50%)."""
    await start(dut)
    for sel, div in DIV.items():
        dut._log.info(f"SEL={sel} -> divisor /{div}, periodo {PERIOD * div} ciclos")
        drive(dut, duty_in=0x08, en=0, sel=sel)   # in3 = 50%
        await ClockCycles(dut.clk, 6)
        drive(dut, en=1)
        await wait_period_end(dut, 3 * PERIOD * div)
        high, n, s = await measure_period(dut, div)
        assert high == THR[3] * div, f"/{div}: alto={high}, esperado={THR[3] * div}"
        assert (s[-1] >> PEND) & 1 == 1, "period_end debe caer en el ultimo ciclo"
        assert sum((v >> PEND) & 1 for v in s) == 1
        # un tick por paso del contador
        assert sum((v >> TICK) & 1 for v in s) == PERIOD, f"/{div}: ticks por periodo incorrectos"
        drive(dut, en=0)
        await ClockCycles(dut.clk, 6)


@cocotb.test()
async def test_enable(dut):
    """EN a 0 limpia contador/PWM; al re-habilitar el primer periodo es completo."""
    await start(dut)
    drive(dut, duty_in=0x04, en=1, sel=1)          # in2 = 35%, /8
    await wait_period_end(dut, 3 * PERIOD * 8)
    await ClockCycles(dut.clk, 300)                 # a mitad de periodo

    drive(dut, en=0)
    await ClockCycles(dut.clk, 6)
    s = await sample(dut, 50)
    assert all(((v >> PWM) & 1) == 0 for v in s), "PWM debe apagarse con EN=0"
    assert all(((v >> MSB) & 1) == 0 for v in s), "El contador debe limpiarse con EN=0"
    assert all(((v >> TICK) & 1) == 0 for v in s)
    assert all(((v >> EN_O) & 1) == 0 for v in s)

    # Re-habilitar en un instante arbitrario del prescaler: el primer periodo debe ser exacto
    await ClockCycles(dut.clk, 3)
    drive(dut, en=1)
    for _ in range(20):
        await RisingEdge(dut.clk)
        if bit(dut.uo_out, PWM):
            break
    else:
        assert False, "El PWM no arranco tras habilitar EN"
    # esta muestra ya es el primer ciclo del periodo; tomamos el resto
    s = [int(dut.uo_out.value)] + await sample(dut, PERIOD * 8 - 1)
    high = sum((v >> PWM) & 1 for v in s)
    assert high == THR[2] * 8, f"Primer periodo tras EN: alto={high}, esperado={THR[2] * 8}"
    assert (s[-1] >> PEND) & 1 == 1, "El primer periodo debe medir exactamente 256*8 ciclos"
    assert sum((v >> PEND) & 1 for v in s) == 1


@cocotb.test()
async def test_change_in_flight(dut):
    """Cambiar duty o SEL a mitad de periodo se aplica hasta el siguiente periodo."""
    await start(dut)
    drive(dut, duty_in=0x02, en=1, sel=0)          # in1 = 25%
    await wait_period_end(dut, 3 * PERIOD)

    # --- Cambio de duty a mitad del periodo en curso ---
    s = await sample(dut, 100)
    drive(dut, duty_in=0x20)                        # in5 = 75%
    s += await sample(dut, PERIOD - 100)
    high = sum((v >> PWM) & 1 for v in s)
    assert high == THR[1], f"Periodo en curso alterado: alto={high}, esperado={THR[1]}"
    assert (s[-1] >> PEND) & 1 == 1
    high, n, s = await measure_period(dut, 1)
    assert high == THR[5], f"Siguiente periodo: alto={high}, esperado={THR[5]}"

    # --- Cambio de SEL (/1 -> /8) a mitad del periodo en curso ---
    s = await sample(dut, 50)
    drive(dut, sel=1)
    s += await sample(dut, PERIOD - 50)
    assert (s[-1] >> PEND) & 1 == 1, "El periodo en curso debe terminar a los 256 ciclos"
    assert sum((v >> PEND) & 1 for v in s) == 1
    high, n, s = await measure_period(dut, 8)
    assert high == THR[5] * 8
    assert (s[-1] >> PEND) & 1 == 1 and sum((v >> PEND) & 1 for v in s) == 1


@cocotb.test()
async def test_reset_mid_operation(dut):
    """Un reset en plena operacion apaga todo y, al soltarlo con EN=1, reinicia limpio."""
    await start(dut)
    drive(dut, duty_in=0x10, en=1, sel=0)          # in4 = 65%
    await wait_period_end(dut, 3 * PERIOD)
    await ClockCycles(dut.clk, 60)

    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)                   # 2 de sincronizador + 1 de registro
    for _ in range(5):
        await RisingEdge(dut.clk)
        assert int(dut.uo_out.value) & 0b0000_1111 == 0b0000_0010, \
            "En reset: PWM=0, PWM_N=1, period_end=0, tick=0"
    dut.rst_n.value = 1

    # El primer periodo tras el reset ya debe tener el duty correcto.
    # (El reset se libera ~3 ciclos despues de rst_n=1: sincronizador + registro)
    for _ in range(10):
        await RisingEdge(dut.clk)
        if bit(dut.uo_out, PWM):
            break
    else:
        assert False, "El PWM no reinicio tras el reset"
    s = [int(dut.uo_out.value)] + await sample(dut, PERIOD - 1)
    high = sum((v >> PWM) & 1 for v in s)
    assert high == THR[4], f"Tras reset: alto={high}, esperado={THR[4]}"
    assert (s[-1] >> PEND) & 1 == 1


@cocotb.test()
async def test_io_mapping(dut):
    """Los pines uio_out[7:3] reflejan las salidas esperadas; uio_oe = 1111_1000."""
    await start(dut)
    drive(dut, duty_in=0x08, en=1, sel=0)
    await wait_period_end(dut, 3 * PERIOD)
    for _ in range(2 * PERIOD):
        await RisingEdge(dut.clk)
        uo, uio = int(dut.uo_out.value), int(dut.uio_out.value)
        assert ((uio >> 3) & 1) == ((uo >> PWM) & 1), "uio[3] = PWM"
        assert ((uio >> 4) & 1) == ((uo >> PWM_N) & 1), "uio[4] = PWM negado"
        assert ((uio >> 5) & 1) == ((uo >> PEND) & 1), "uio[5] = fin de periodo"
        assert ((uio >> 6) & 1) == ((uo >> MSB) & 1), "uio[6] = MSB del contador"
        assert ((uio >> 7) & 1) == ((uo >> TICK) & 1), "uio[7] = tick"
        assert (uio & 0b111) == 0, "Pines de entrada no deben manejar nivel"
    assert int(dut.uio_oe.value) == 0b11111000


@cocotb.test()
async def test_msb_aligned_with_pwm(dut):
    """El MSB del contador (uo_out[5]) cambia justo cuando el PWM de 50% baja."""
    await start(dut)
    drive(dut, duty_in=0x08, en=1, sel=0)          # 50% -> umbral 128 = MSB
    await wait_period_end(dut, 3 * PERIOD)
    s = await sample(dut, PERIOD)
    for k, v in enumerate(s):
        assert ((v >> PWM) & 1) == (1 if k < 128 else 0)
        assert ((v >> MSB) & 1) == (0 if k < 128 else 1), f"MSB desalineado en ciclo {k}"
