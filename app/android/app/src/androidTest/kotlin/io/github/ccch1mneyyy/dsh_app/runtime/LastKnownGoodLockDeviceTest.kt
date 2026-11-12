package io.github.ccch1mneyyy.dsh_app.runtime

import android.test.AndroidTestCase
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.nio.file.Files
import java.nio.file.LinkOption.NOFOLLOW_LINKS
import java.nio.file.Path

/**
 * Device-only restore cleanup and dead-pid takeover contracts, using Android's real process API.
 * These cases can also pass with the old publisher; they do not probe its process-kill window.
 */
@Suppress("DEPRECATION")
class LastKnownGoodLockDeviceTest : AndroidTestCase() {
    private val good = ProfileSnapshot(listOf("core"), emptySet(), emptyMap(), null)

    fun testRestoreChangesBundlesAndRemovesItsLockAndOwner() {
        withProfile { root, dir, pkg ->
            val before = pkg.readBytes()

            LastKnownGood.restore(root, good, "success")

            val bundles = JSONObject(pkg.readText()).getJSONObject("dsh")
                .getJSONObject("profile").getJSONArray("bundles")
            assertEquals(1, bundles.length())
            assertEquals("core", bundles.getString(0))
            assertFalse(before.contentEquals(pkg.readBytes()))
            assertFalse(Files.exists(File(dir, "package.json.lock").toPath(), NOFOLLOW_LINKS))
            assertEquals(emptySet<String>(), ownerNames(dir))
        }
    }

    fun testMalformedProfileKeepsItsBytesAndRemovesItsLockAndOwner() {
        withProfile { root, dir, pkg ->
            val before = "{".toByteArray(Charsets.UTF_8)
            pkg.writeBytes(before)

            try {
                LastKnownGood.restore(root, good, "malformed")
                fail("malformed profile must fail while holding the lock")
            } catch (_: JSONException) {
                // profileSource accepts this readable file; JSONObject fails inside withLock.
            }

            assertTrue(before.contentEquals(pkg.readBytes()))
            assertFalse(Files.exists(File(dir, "package.json.lock").toPath(), NOFOLLOW_LINKS))
            assertEquals(emptySet<String>(), ownerNames(dir))
        }
    }

    fun testDeadPidSymlinkIsTakenOverWithoutDeletingItsOldOwner() {
        withProfile { root, dir, pkg ->
            val deadPid = Int.MAX_VALUE
            assertTrue(deadPid > 0)
            assertFalse(Files.exists(File("/proc/$deadPid").toPath(), NOFOLLOW_LINKS))
            val oldOwner = File(dir, ".dsh-lkg-lock-old.pid")
            val oldBytes = "$deadPid\n".toByteArray(Charsets.UTF_8)
            oldOwner.writeBytes(oldBytes)
            val lock = File(dir, "package.json.lock").toPath()
            Files.createSymbolicLink(lock, oldOwner.toPath().fileName)
            assertEquals(oldOwner.toPath().fileName, Files.readSymbolicLink(lock))

            LastKnownGood.restore(root, good, "takeover")

            val bundles = JSONObject(pkg.readText()).getJSONObject("dsh")
                .getJSONObject("profile").getJSONArray("bundles")
            assertEquals(1, bundles.length())
            assertEquals("core", bundles.getString(0))
            assertFalse(Files.exists(lock, NOFOLLOW_LINKS))
            assertTrue(oldBytes.contentEquals(oldOwner.readBytes()))
            assertEquals(setOf(oldOwner.name), ownerNames(dir))
        }
    }

    private fun withProfile(body: (File, File, File) -> Unit) {
        val base = Files.createTempDirectory(context.cacheDir.toPath(), "lkg-lock-device-")
        try {
            val root = File(base.toFile(), "rootfs")
            val dir = LastKnownGood.webDir(root)
            check(dir.mkdirs())
            val pkg = File(dir, "package.json").apply {
                writeText("""{"dependencies":{},"dsh":{"profile":{"bundles":["core","extra"]}}}""")
            }
            body(root, dir, pkg)
        } finally {
            deleteNoFollow(base)
        }
    }

    private fun ownerNames(dir: File): Set<String> =
        Files.newDirectoryStream(dir.toPath(), ".dsh-lkg-lock-*").use { entries ->
            entries.map { it.fileName.toString() }.toSet()
        }

    private fun deleteNoFollow(path: Path) {
        if (Files.isDirectory(path, NOFOLLOW_LINKS)) {
            Files.newDirectoryStream(path).use { entries ->
                for (entry in entries) deleteNoFollow(entry)
            }
        }
        Files.deleteIfExists(path)
    }
}
