package io.github.ccch1mneyyy.dsh_app.update.privileged

import android.os.ParcelFileDescriptor
import android.system.Os
import android.system.OsConstants
import java.io.Closeable
import java.io.File
import java.io.InputStream
import java.util.concurrent.atomic.AtomicBoolean

/** Native-only one-shot capability; no caller-supplied command, package or installer identity. */
class PreparedSelfUpdate internal constructor(
    val versionName: String,
    val versionCode: Long,
    val sha256: String,
    val backend: String,
    val uid: Int,
    internal val owner: Any,
    internal val snapshot: SelfUpdateSnapshot,
    internal val identity: SelfUpdateIdentity,
    internal val transport: SelfUpdateTransport,
    internal val createdNanos: Long,
    private val onClose: (PreparedSelfUpdate) -> Unit,
) : Closeable {
    val digest: String get() = sha256
    internal val consumed = AtomicBoolean(false)
    internal val closed = AtomicBoolean(false)
    internal val submitted = AtomicBoolean(false)

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        runCatching { transport.close() }
        runCatching { snapshot.close() }
        onClose(this)
    }
}

internal class SelfUpdateSnapshot(source: File, val size: Long, private val unlinkOnOpen: Boolean = false) : Closeable {
    private val descriptor = ParcelFileDescriptor.open(source, ParcelFileDescriptor.MODE_READ_ONLY)
    private val initial = try {
        Os.fstat(descriptor.fileDescriptor).also { if (unlinkOnOpen) Os.unlink(source.path) }
    } catch (error: Exception) {
        runCatching { descriptor.close() }
        throw error
    }
    val file: File = if (unlinkOnOpen) File("/proc/self/fd/${descriptor.fd}") else source

    fun verifyPath() {
        val held = Os.fstat(descriptor.fileDescriptor)
        val current = if (unlinkOnOpen) held else Os.lstat(file.path)
        if (!OsConstants.S_ISREG(current.st_mode) || current.st_dev != initial.st_dev ||
            current.st_ino != initial.st_ino || current.st_size != size ||
            held.st_size != size)
            throw SelfUpdateException("snapshotChanged")
    }

    fun duplicate(): ParcelFileDescriptor {
        verifyPath()
        return ParcelFileDescriptor.dup(descriptor.fileDescriptor).also {
            Os.lseek(it.fileDescriptor, 0, OsConstants.SEEK_SET)
        }
    }

    fun input(): InputStream = ParcelFileDescriptor.AutoCloseInputStream(duplicate())

    fun verifyHash(expected: String) {
        verifyPath()
        val actual = input().use { SelfUpdatePolicy.hash(it) }
        verifyPath()
        if (actual != expected) throw SelfUpdateException("hashMismatch")
    }

    override fun close() {
        runCatching { descriptor.close() }
        // An unlinked snapshot belongs only to the held descriptor, never to a later path occupant.
        if (!unlinkOnOpen) file.delete()
    }
}

internal interface SelfUpdateTransport : Closeable {
    val uid: Int
    fun verifyIdentity()
    fun install(prepared: PreparedSelfUpdate, userId: Int, requestId: String): SelfUpdateReceipt
}
