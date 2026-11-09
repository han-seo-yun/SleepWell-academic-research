package app.sleepwell.analysis

import java.io.File
import java.io.FileOutputStream
import java.io.InputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/** Keeps the calculation CSV in app-persistent storage and replaces it atomically. */
object CalculationInputStore {
    const val RELATIVE_PATH = "work/upload_sleep_stage.csv"

    fun workingFile(filesDir: File): File = File(filesDir, RELATIVE_PATH)

    /** Deletes only the persistent calculation copy when the user clears original sleep data. */
    fun clear(filesDir: File, legacyCacheFile: File? = null) {
        check(legacyCacheFile == null || !legacyCacheFile.exists() || legacyCacheFile.delete()) {
            "이전 계산용 수면 기록을 지우지 못했어요."
        }
        val target = workingFile(filesDir)
        check(!target.exists() || target.delete()) { "계산용 수면 기록을 지우지 못했어요." }
    }

    /** Migrates the former cache copy once. It never creates an empty or demo input. */
    fun prepare(filesDir: File, legacyCacheFile: File?): File? {
        val target = workingFile(filesDir)
        if (target.isFile) return target
        if (legacyCacheFile?.isFile != true) return null
        return replaceFrom(legacyCacheFile, filesDir)
    }

    fun replaceFrom(source: File, filesDir: File): File =
        source.inputStream().use { replaceFrom(it, filesDir) }

    fun replaceFrom(input: InputStream, filesDir: File): File {
        val target = workingFile(filesDir)
        target.parentFile?.mkdirs()
        val temporary = File(target.parentFile, target.name + ".tmp-${System.nanoTime()}")
        try {
            FileOutputStream(temporary).use { output ->
                input.copyTo(output)
                output.fd.sync()
            }
            check(temporary.length() > 0L) { "계산용 CSV가 비어 있어 기존 파일을 유지했어요." }
            try {
                Files.move(
                    temporary.toPath(), target.toPath(),
                    StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING
                )
            } catch (_: AtomicMoveNotSupportedException) {
                Files.move(temporary.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
            }
            return target
        } finally {
            if (temporary.exists()) temporary.delete()
        }
    }
}
