package app.sleepwell.ui

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import app.sleepwell.health.DisplaySettings
import app.sleepwell.health.DisplayZone
import app.sleepwell.analysis.NextNightModel
import java.time.ZoneId
import kotlinx.coroutines.*

/**
 * 기기 시간대를 따르는 화면 상태.
 *
 * Compose 상태로 들고 있어서, 화면에서 `UiZone.current` 를 읽은 곳은 시간대를 바꾸면 자동으로 다시 그려진다.
 * 바꿀 때 DisplaySettings.zone 도 함께 바꿔서 입력·계산·표시가 같은 시간대를 쓰게 한다.
 * 원시 기록과 기존 export의 절대 시각은 바꾸지 않는다.
 */
object UiZone {
    private var state by mutableStateOf(DisplaySettings.zone)
    private val refreshScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var refreshJob: Job? = null
    private var requestedZone: String? = null

    internal suspend fun awaitRefresh() { refreshJob?.join() }

    val current: DisplayZone get() = state

    /** MainActivity의 시작·재개·시간대 변경 수신 시 메인 스레드에서 호출한다. */
    fun refreshFromSystem() {
        val current = DisplayZone.systemDefault()
        if (!NextNightModel.isInstalled) {
            NextNightModel.refreshCalculationZone(ZoneId.of(current.zoneId))
            DisplaySettings.zone = current
            state = current
            return
        }
        if (current == state && NextNightModel.currentCalculationZone().id == current.zoneId && refreshJob?.isActive != true) return
        if (requestedZone == current.zoneId && refreshJob?.isActive == true) return
        requestedZone = current.zoneId
        refreshJob?.cancel()
        refreshJob = refreshScope.launch {
            // An offline inference holds the engine lock for minutes. Never wait on the UI thread.
            withContext(Dispatchers.Default) {
                NextNightModel.refreshCalculationZone(ZoneId.of(current.zoneId))
            }
            // Publish calculation and display zones together; cancellation discards older requests.
            DisplaySettings.zone = current
            state = current
        }
    }
}
