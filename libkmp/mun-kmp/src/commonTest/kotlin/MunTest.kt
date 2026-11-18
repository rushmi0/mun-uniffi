import org.siamdev.mun.add
import kotlin.test.Test
import kotlin.test.assertEquals

class MunTest {

    @Test
    fun addsAcrossFfi() {
        assertEquals(4uL, add(2uL, 2uL))
    }

}
