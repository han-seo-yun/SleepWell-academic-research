package app.sleepwell.analysis

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.SystemClock
import android.test.InstrumentationTestCase
import android.view.accessibility.AccessibilityNodeInfo
import app.sleepwell.MainActivity
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import app.sleepwell.ui.HealthPermissionsRationaleActivity
import app.sleepwell.ui.ProfileEditor
import app.sleepwell.ui.SleepWellTheme
import java.io.File

/** Launch/render smoke tests. All artificial profiles stay in memory; user files are read-only. */
@Suppress("DEPRECATION")
class V7LaunchRegressionTest : InstrumentationTestCase() {
    fun testFirstRunRendersWithoutAnySleepFixture() = launchWithProfile(false)

    fun testDashboardRendersWithoutCreatingDemoRecords() = launchWithProfile(true)

    fun testFirstRunWithoutAgeOrOccupationEnablesNextAndPreservesEmptyAgeOnSave() {
        val context = instrumentation.targetContext
        val protectedFiles = listOf(ProfileStore.FILE_NAME, "survey_log.csv", "saved_results.csv", DiaryStore.FILE_NAME)
            .associateWith { name -> File(context.filesDir, name).takeIf { it.exists() }?.readBytes() }
        var activity: Activity? = null
        var saved: NextNightModel.ModelProfile? = null
        try {
            activity = instrumentation.startActivitySync(Intent(context, HealthPermissionsRationaleActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            val host = activity as ComponentActivity
            instrumentation.runOnMainSync {
                host.setContent {
                    SleepWellTheme {
                        ProfileEditor(
                            initialProfile = NextNightModel.ModelProfile(
                                ageBand = "", sex = "female", occupation = "", onboardingCompleted = false
                            ),
                            firstRun = true,
                            onSave = { saved = it; null }
                        )
                    }
                }
            }
            instrumentation.waitForIdleSync()
            if (waitForExactText("기본정보") == null)
                fail("최초 설정 화면이 렌더링돼야 합니다.\n${diagnostics("renderer")}")
            assertNull("직업 입력은 최초 설정에 표시하지 않아야 합니다.",
                findExactTextWithScrolling("직업군"))
            assertNull("연령대 입력은 최초 설정에 표시하지 않아야 합니다.",
                findExactTextWithScrolling("연령대"))
            ProfileStore.AGE_BANDS.forEach { age ->
                assertNull("연령대 선택 항목은 표시하지 않아야 합니다: $age",
                    findExactTextWithScrolling("${age}세"))
            }
            val next = clickableAncestor(findExactTextWithScrolling("다음 · 수면 습관 입력"))
            if (next == null) fail("다음 버튼을 찾을 수 있어야 합니다.\n${diagnostics("next-button")}")
            val nextButton = next ?: throw AssertionError("다음 버튼을 찾을 수 있어야 합니다.")
            assertTrue("연령대와 직업 없이 성별만으로 다음 버튼이 활성화돼야 합니다.", nextButton.isEnabled)
            assertTrue(nextButton.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            instrumentation.waitForIdleSync()
            if (waitForExactTextWithScrolling("희망 수면시간") == null)
                fail("수면 습관 단계가 렌더링돼야 합니다.\n${diagnostics("sleep-step")}")
            val save = clickableAncestor(findExactTextWithScrolling("저장하고 시작하기"))
            if (save == null) fail("저장 버튼을 찾을 수 있어야 합니다.\n${diagnostics("save-button")}")
            val saveButton = save ?: throw AssertionError("저장 버튼을 찾을 수 있어야 합니다.")
            assertTrue(saveButton.isEnabled)
            assertTrue(saveButton.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            instrumentation.waitForIdleSync()
            assertNotNull(saved)
            assertEquals("", saved!!.ageBand)
            protectedFiles.forEach { (name, original) ->
                val current = File(context.filesDir, name).takeIf { it.exists() }?.readBytes()
                assertTrue("UI 검증은 $name 을 변경하면 안 됩니다.", if (original == null) current == null
                    else current != null && original.contentEquals(current))
            }
        } finally {
            activity?.let { opened -> instrumentation.runOnMainSync { opened.finish() } }
            instrumentation.waitForIdleSync()
        }
    }

    fun testEditingLegacyProfileDoesNotShowAgeAndPreservesStoredAge() {
        val context = instrumentation.targetContext
        var activity: Activity? = null
        var saved: NextNightModel.ModelProfile? = null
        try {
            activity = instrumentation.startActivitySync(Intent(context, HealthPermissionsRationaleActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            val host = activity as ComponentActivity
            instrumentation.runOnMainSync {
                host.setContent {
                    SleepWellTheme {
                        ProfileEditor(
                            initialProfile = NextNightModel.ModelProfile(
                                ageBand = "60-80", sex = "female", onboardingCompleted = true
                            ),
                            firstRun = false,
                            onSave = { saved = it; null }
                        )
                    }
                }
            }
            instrumentation.waitForIdleSync()
            if (waitForExactText("내 정보 수정") == null)
                fail("프로필 수정 화면이 렌더링돼야 합니다.\n${diagnostics("legacy-renderer")}")
            assertNull("저장된 연령대도 편집 입력으로 다시 노출하지 않습니다.",
                findExactTextWithScrolling("연령대"))
            assertNull(findExactTextWithScrolling("60-80세"))
            val save = clickableAncestor(waitForExactTextWithScrolling("저장하고 다시 계산"))
            if (save == null) fail("수정 저장 버튼을 찾을 수 있어야 합니다.\n${diagnostics("legacy-save")}")
            val saveButton = save ?: throw AssertionError("수정 저장 버튼을 찾을 수 있어야 합니다.")
            assertTrue(saveButton.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            instrumentation.waitForIdleSync()
            assertNotNull(saved)
            assertEquals("60-80", saved!!.ageBand)
        } finally {
            activity?.let { opened -> instrumentation.runOnMainSync { opened.finish() } }
            instrumentation.waitForIdleSync()
        }
    }

    private fun launchWithProfile(completed: Boolean) {
        val context = instrumentation.targetContext
        val protectedFiles = listOf(ProfileStore.FILE_NAME, "survey_log.csv", "saved_results.csv", DiaryStore.FILE_NAME)
            .associateWith { name -> File(context.filesDir, name).takeIf { it.exists() }?.readBytes() }
        var activity: Activity? = null
        try {
            val hostClass = if (completed) MainActivity::class.java else HealthPermissionsRationaleActivity::class.java
            activity = instrumentation.startActivitySync(Intent(context, hostClass)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            if (!completed) {
                val host = activity as ComponentActivity
                instrumentation.runOnMainSync {
                    host.setContent {
                        SleepWellTheme {
                            ProfileEditor(
                                initialProfile = NextNightModel.ModelProfile(onboardingCompleted = false),
                                firstRun = true,
                                onSave = { "검증 화면에서는 저장하지 않습니다." }
                            )
                        }
                    }
                }
            }
            instrumentation.waitForIdleSync()
            assertNotNull(activity)
            assertFalse(activity.isFinishing)
            assertFalse(activity.isDestroyed)
            instrumentation.runOnMainSync { assertTrue(activity.window.decorView.isShown) }
            if (completed) assertFalse("User launch must never select a demo model", NextNightModel.demoMode)
            protectedFiles.forEach { (name, original) ->
                val current = File(context.filesDir, name).takeIf { it.exists() }?.readBytes()
                assertTrue("Launching must preserve $name", if (original == null) current == null
                    else current != null && original.contentEquals(current))
            }
        } finally {
            activity?.let { opened -> instrumentation.runOnMainSync { opened.finish() } }
            instrumentation.waitForIdleSync()
        }
    }

    private fun findExactText(root: AccessibilityNodeInfo?, text: String): AccessibilityNodeInfo? {
        if (root == null) return null
        if (root.text?.toString() == text || root.contentDescription?.toString() == text) return root
        for (index in 0 until root.childCount) {
            findExactText(root.getChild(index), text)?.let { return it }
        }
        return null
    }

    private fun waitForExactText(text: String, timeoutMs: Long = 3_000): AccessibilityNodeInfo? {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        do {
            findExactText(instrumentation.uiAutomation.rootInActiveWindow, text)?.let { return it }
            SystemClock.sleep(50)
        } while (SystemClock.elapsedRealtime() < deadline)
        return null
    }

    private fun waitForExactTextWithScrolling(text: String, timeoutMs: Long = 3_000): AccessibilityNodeInfo? {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        do {
            findExactTextWithScrolling(text)?.let { return it }
            SystemClock.sleep(50)
        } while (SystemClock.elapsedRealtime() < deadline)
        return null
    }

    /** Compose exposes only the visible portion of a verticalScroll column to accessibility. */
    private fun findExactTextWithScrolling(text: String): AccessibilityNodeInfo? {
        findExactText(instrumentation.uiAutomation.rootInActiveWindow, text)?.let { return it }
        scrollUntilFound(text, AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)?.let { return it }
        return scrollUntilFound(text, AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
    }

    private fun scrollUntilFound(text: String, action: Int): AccessibilityNodeInfo? {
        repeat(12) {
            val root = instrumentation.uiAutomation.rootInActiveWindow ?: return null
            findExactText(root, text)?.let { return it }
            val scroller = findScrollable(root) ?: return null
            if (!scroller.performAction(action)) return null
            instrumentation.waitForIdleSync()
        }
        return findExactText(instrumentation.uiAutomation.rootInActiveWindow, text)
    }

    private fun findScrollable(node: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        if (node == null) return null
        if (node.isScrollable) return node
        for (index in 0 until node.childCount) {
            findScrollable(node.getChild(index))?.let { return it }
        }
        return null
    }

    private fun diagnostics(label: String): String {
        val screenshot = File(instrumentation.context.cacheDir,
            "v7-launch-$label-${System.nanoTime()}.png")
        runCatching {
            screenshot.outputStream().use { output ->
                instrumentation.uiAutomation.takeScreenshot()
                    .compress(Bitmap.CompressFormat.PNG, 100, output)
            }
        }
        val tree = StringBuilder()
        dumpNode(instrumentation.uiAutomation.rootInActiveWindow, tree, 0, intArrayOf(0))
        return "screenshot=${screenshot.absolutePath}\naccessibility=${tree.take(12_000)}"
    }

    private fun dumpNode(node: AccessibilityNodeInfo?, out: StringBuilder, depth: Int, count: IntArray) {
        if (node == null || depth > 14 || count[0] >= 250) return
        count[0]++
        val bounds = Rect().also(node::getBoundsInScreen)
        out.append('\n').append("  ".repeat(depth))
            .append(node.className).append(" text=").append(node.text)
            .append(" desc=").append(node.contentDescription)
            .append(" enabled=").append(node.isEnabled)
            .append(" clickable=").append(node.isClickable)
            .append(" scrollable=").append(node.isScrollable)
            .append(" bounds=").append(bounds)
        for (index in 0 until node.childCount) dumpNode(node.getChild(index), out, depth + 1, count)
    }

    private fun clickableAncestor(node: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        var current = node
        while (current != null && !current.isClickable) current = current.parent
        return current
    }
}
