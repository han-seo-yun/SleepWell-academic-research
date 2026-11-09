package org.json

import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonPrimitive

private val gson = Gson()

private fun jsonValue(value: Any?): JsonElement = when (value) {
    null, JSONObject.NULL -> JsonNull.INSTANCE
    is JSONObject -> value.raw
    is JSONArray -> value.raw
    is JsonElement -> value
    is Boolean -> JsonPrimitive(value)
    is Number -> JsonPrimitive(value)
    is String -> JsonPrimitive(value)
    else -> gson.toJsonTree(value)
}

class JSONObject internal constructor(internal val raw: JsonObject) {
    constructor() : this(JsonObject())
    constructor(text: String) : this(JsonParser.parseString(text).asJsonObject)
    constructor(values: Map<*, *>) : this(JsonObject().also { out ->
        values.forEach { (key, value) -> out.add(key.toString(), jsonValue(value)) }
    })

    fun put(key: String, value: Any?): JSONObject = apply { raw.add(key, jsonValue(value)) }
    fun has(key: String): Boolean = raw.has(key)
    fun isNull(key: String): Boolean = !raw.has(key) || raw.get(key).isJsonNull
    fun length(): Int = raw.size()
    fun keys(): MutableIterator<String> = raw.keySet().iterator()
    fun getString(key: String): String = required(key).asString
    fun getInt(key: String): Int = required(key).asInt
    fun getDouble(key: String): Double = required(key).asDouble
    fun getBoolean(key: String): Boolean = required(key).asBoolean
    fun getJSONObject(key: String): JSONObject = JSONObject(required(key).asJsonObject)
    fun getJSONArray(key: String): JSONArray = JSONArray(required(key).asJsonArray)
    fun optString(key: String): String = optString(key, "")
    fun optString(key: String, fallback: String): String = raw.get(key)?.takeUnless { it.isJsonNull }?.asString ?: fallback
    fun optBoolean(key: String): Boolean = optBoolean(key, false)
    fun optBoolean(key: String, fallback: Boolean): Boolean = raw.get(key)?.takeUnless { it.isJsonNull }?.asBoolean ?: fallback
    fun optJSONObject(key: String): JSONObject? = raw.get(key)?.takeIf { it.isJsonObject }?.asJsonObject?.let(::JSONObject)
    fun optJSONArray(key: String): JSONArray? = raw.get(key)?.takeIf { it.isJsonArray }?.asJsonArray?.let(::JSONArray)
    private fun required(key: String): JsonElement = raw.get(key) ?: error("Missing JSON key: $key")
    override fun toString(): String = gson.toJson(raw)

    companion object {
        val NULL: Any = object { override fun toString(): String = "null" }
    }
}

class JSONArray internal constructor(internal val raw: JsonArray) {
    constructor() : this(JsonArray())
    constructor(text: String) : this(JsonParser.parseString(text).asJsonArray)
    fun put(value: Any?): JSONArray = apply { raw.add(jsonValue(value)) }
    fun length(): Int = raw.size()
    fun getString(index: Int): String = raw[index].asString
    fun getInt(index: Int): Int = raw[index].asInt
    fun getDouble(index: Int): Double = raw[index].asDouble
    fun getBoolean(index: Int): Boolean = raw[index].asBoolean
    fun getJSONObject(index: Int): JSONObject = JSONObject(raw[index].asJsonObject)
    fun getJSONArray(index: Int): JSONArray = JSONArray(raw[index].asJsonArray)
    override fun toString(): String = gson.toJson(raw)
}
