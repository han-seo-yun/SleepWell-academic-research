package app.sleepwell.analysis

import app.sleepwell.ui.ConnectionVisualState
import app.sleepwell.ui.connectionVisualState
import junit.framework.TestCase

class ConnectionUiRegressionTest : TestCase() {
    fun testConnectionDoesNotImplyReceivedRecords() {
        assertEquals(ConnectionVisualState.EMPTY, connectionVisualState(true, false, false, 0, null, 900_000_000L))
        assertEquals(ConnectionVisualState.DISCONNECTED, connectionVisualState(false, false, false, 5, 1L, 900_000_000L))
    }
    fun testFailuresAndSyncHaveExplicitPriority() {
        assertEquals(ConnectionVisualState.ERROR, connectionVisualState(true, false, true, 4, 1L, 900_000_000L))
        assertEquals(ConnectionVisualState.SYNCING, connectionVisualState(true, true, true, 4, 1L, 900_000_000L))
    }
    fun testOldRecordsRemainVisibleButStale() {
        assertEquals(ConnectionVisualState.STALE, connectionVisualState(true, false, false, 4, 1L, 900_000_000L))
        assertEquals(ConnectionVisualState.READY, connectionVisualState(true, false, false, 4, 899_000_000L, 900_000_000L))
    }
}
