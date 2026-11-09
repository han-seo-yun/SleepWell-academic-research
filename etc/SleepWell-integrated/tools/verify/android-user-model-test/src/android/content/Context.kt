package android.content

import java.io.InputStream

open class Context {
    open val assets: AssetManager
        get() = error("Context assets are unavailable in the JVM verification harness")
}

open class AssetManager {
    open fun open(path: String): InputStream = error("Missing test asset: $path")
}
