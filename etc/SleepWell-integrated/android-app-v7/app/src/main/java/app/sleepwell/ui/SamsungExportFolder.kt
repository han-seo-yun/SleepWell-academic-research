package app.sleepwell.ui

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract

/*
 * 삼성 헬스 내보내기 폴더에서 수면 단계 CSV 찾기 (소빈 · 2026-10-27)
 *
 * 안드로이드 기본 파일 화면은 "파일 이름으로 거르기"가 안 돼서, 삼성 헬스 CSV 가 수십 개 보이고
 * 그중 어떤 걸 골라야 할지 헷갈린다 → 사용자는 **폴더**를 고르고, 앱이 그 안에서
 * com.samsung.health.sleep_stage.<번호>.csv 를 찾아 읽는다.
 *
 * - 고른 폴더 바로 안 + 하위 폴더 3단계까지 찾는다 ("Samsung Health" 폴더를 골라도, 그 안의 날짜 폴더를 골라도 됨)
 * - 여러 개면(여러 번 내보낸 경우) 이름의 번호가 가장 큰 것 = 가장 최근 내보내기
 * - 추가 라이브러리 없이 DocumentsContract 만 쓴다
 */
object SamsungExportFolder {

    private const val PREFIX = "com.samsung.health.sleep_stage"

    data class Found(val uri: Uri, val name: String)

    fun findSleepStageCsv(context: Context, treeUri: Uri, maxDepth: Int = 3): Found? {
        val rootId = DocumentsContract.getTreeDocumentId(treeUri)
        val hits = mutableListOf<Found>()
        fun walk(docId: String, depth: Int) {
            val children = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, docId)
            val cols = arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE
            )
            context.contentResolver.query(children, cols, null, null, null)?.use { c ->
                while (c.moveToNext()) {
                    val id = c.getString(0) ?: continue
                    val name = c.getString(1) ?: ""
                    val mime = c.getString(2) ?: ""
                    if (mime == DocumentsContract.Document.MIME_TYPE_DIR) {
                        if (depth < maxDepth) walk(id, depth + 1)
                    } else if (name.startsWith(PREFIX) && name.endsWith(".csv", ignoreCase = true)) {
                        hits += Found(DocumentsContract.buildDocumentUriUsingTree(treeUri, id), name)
                    }
                }
            }
        }
        walk(rootId, 0)
        return hits.maxByOrNull { it.name }
    }
}
