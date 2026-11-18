import org.siamdev.mun.HostFunction
import org.siamdev.mun.MunEngine
import org.siamdev.mun.MunException
import org.siamdev.mun.MunType
import org.siamdev.mun.MunValue
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull

class MunEngineTest {

    @Test
    fun evalRunsMain() {
        MunEngine().use { engine ->
            assertEquals(MunValue.I32(42), engine.eval("pub fn main() -> i32 { 40 + 2 }"))
        }
    }

    @Test
    fun hotReloadsUpdatedSource() {
        MunEngine().use { engine ->
            val args = listOf(MunValue.I32(6), MunValue.I32(7))

            assertNull(engine.eval("pub fn op(a: i32, b: i32) -> i32 { a + b }"))
            assertEquals(MunValue.I32(13), engine.call("op", args))

            // Same engine, new code: the runtime reloads the recompiled library in place.
            engine.eval("pub fn op(a: i32, b: i32) -> i32 { a * b }")
            assertEquals(MunValue.I32(42), engine.call("op", args))
        }
    }

    @Test
    fun compileErrorKeepsPreviousCode() {
        MunEngine().use { engine ->
            engine.eval("pub fn answer() -> f64 { 4.2 }")

            assertFailsWith<MunException.Compile> {
                engine.eval("pub fn answer() -> f64 { true }")
            }
            assertEquals(MunValue.F64(4.2), engine.call("answer", emptyList()))
        }
    }

    @Test
    fun callRejectsMismatchedArguments() {
        MunEngine().use { engine ->
            engine.eval("pub fn negate(b: bool) -> bool { !b }")

            assertEquals(MunValue.Bool(false), engine.call("negate", listOf(MunValue.Bool(true))))
            assertFailsWith<MunException.Invoke> {
                engine.call("negate", listOf(MunValue.I32(1)))
            }
        }
    }

    @Test
    fun externFnCallsKotlin() {
        MunEngine().use { engine ->
            val seen = mutableListOf<MunValue>()
            engine.registerFn("scale", listOf(MunType.F64, MunType.I32), MunType.F64, object : HostFunction {
                override fun call(args: List<MunValue>): MunValue {
                    seen += args
                    val x = (args[0] as MunValue.F64).value
                    val factor = (args[1] as MunValue.I32).value
                    return MunValue.F64(x * factor)
                }
            })

            val result = engine.eval(
                """
                extern fn scale(x: f64, factor: i32) -> f64;
                pub fn main() -> f64 { scale(1.5, 4) + 1.0 }
                """.trimIndent(),
            )
            assertEquals(MunValue.F64(7.0), result)
            assertEquals(listOf<MunValue>(MunValue.F64(1.5), MunValue.I32(4)), seen)
        }
    }

    @Test
    fun kotlinExceptionInExternFnIsReported() {
        MunEngine().use { engine ->
            engine.registerFn("fail", emptyList(), MunType.I32, object : HostFunction {
                override fun call(args: List<MunValue>): MunValue = error("boom")
            })
            engine.eval("extern fn fail() -> i32; pub fn run() -> i32 { fail() }")

            assertFailsWith<MunException.Host> { engine.call("run", emptyList()) }
        }
    }

    /** A `println` host function that records each line it prints in [printed]. */
    private fun printer(printed: MutableList<String>) = object : HostFunction {
        override fun call(args: List<MunValue>): MunValue {
            val line = when (val value = args.single()) {
                is MunValue.Bool -> value.value.toString()
                is MunValue.I32 -> value.value.toString()
                is MunValue.I64 -> value.value.toString()
                is MunValue.F32 -> value.value.toString()
                is MunValue.F64 -> value.value.toString()
                is MunValue.Str -> value.value
                MunValue.Unit -> error("println got Unit")
            }
            println(line)
            printed += line
            return MunValue.Unit
        }
    }

    @Test
    fun externPrintlnPrintsFromMun() {
        MunEngine().use { engine ->
            val printed = mutableListOf<String>()
            // Mun has no strings yet, so `println` prints the number it is given.
            engine.registerFn("println", listOf(MunType.I64), MunType.UNIT, printer(printed))

            val result = engine.eval(
                """
                extern fn println(value: i64);

                pub fn main() {
                    let i = 1;
                    loop {
                        if i > 3 {
                            return
                        }
                        println(i * i);
                        i += 1;
                    }
                }
                """.trimIndent(),
            )

            assertEquals(MunValue.Unit, result)
            assertEquals(listOf("1", "4", "9"), printed)
        }
    }

    @Test
    fun registeredPrintlnNeedsNoDeclaration() {
        MunEngine().use { engine ->
            val printed = mutableListOf<String>()
            engine.registerFn("println", listOf(MunType.F64), MunType.UNIT, printer(printed))

            // The engine declares `extern fn println(arg0: f64);` itself.
            engine.eval("pub fn main() { println(1.5); println(0.25 * 2.0) }")

            assertEquals(listOf("1.5", "0.5"), printed)
        }
    }

    @Test
    fun printlnPerArgumentType() {
        MunEngine().use { engine ->
            val printed = mutableListOf<String>()
            // Mun has no overloading, so each argument type gets its own function.
            val types = mapOf(
                "println_bool" to MunType.BOOL,
                "println_i32" to MunType.I32,
                "println_i64" to MunType.I64,
                "println_f32" to MunType.F32,
                "println_f64" to MunType.F64,
                "println_str" to MunType.STR,
            )
            for ((name, type) in types) {
                engine.registerFn(name, listOf(type), MunType.UNIT, printer(printed))
            }

            // Without string literals, a string is written as its UTF-8 bytes.
            engine.eval(
                """
                pub fn main() {
                    println_bool(1 < 2);
                    println_i32(-7);
                    println_i64(1 << 40);
                    println_f32(0.5);
                    println_f64(2.25);
                    println_str([104u8, 105u8]);
                }
                """.trimIndent(),
            )

            assertEquals(listOf("true", "-7", "1099511627776", "0.5", "2.25", "hi"), printed)
        }
    }

    @Test
    fun printlnSurvivesHotReload() {
        MunEngine().use { engine ->
            val printed = mutableListOf<String>()
            engine.registerFn("println", listOf(MunType.I32), MunType.UNIT, printer(printed))

            engine.eval("pub fn main() { println(1) }")
            engine.eval("pub fn main() { println(2); println(3) }")

            assertEquals(listOf("1", "2", "3"), printed)
        }
    }

    @Test
    fun printlnExceptionIsReported() {
        MunEngine().use { engine ->
            engine.registerFn("println", listOf(MunType.I32), MunType.UNIT, object : HostFunction {
                override fun call(args: List<MunValue>): MunValue = error("stdout closed")
            })

            assertFailsWith<MunException.Host> { engine.eval("pub fn main() { println(1) }") }
        }
    }

    @Test
    fun printlnMustBeRegisteredBeforeEval() {
        MunEngine().use { engine ->
            engine.eval("pub fn main() {}")

            assertFailsWith<MunException.Invoke> {
                engine.registerFn("println", listOf(MunType.I32), MunType.UNIT, printer(mutableListOf()))
            }
        }
    }
}
