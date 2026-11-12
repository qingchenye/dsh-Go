package io.github.ccch1mneyyy.dsh_app.runtime

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.attribute.PosixFilePermissions
import java.util.concurrent.CompletableFuture
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import org.junit.After
import org.junit.Assert.*
import org.junit.Test

class LastKnownGoodLockTest {
    private val directory = kotlin.io.path.createTempDirectory("lkg-writer-lock-").toFile()
    private val file = File(directory, "package.json")
    private val lock = File(file.path + ".lock")
    private val deadPid = 424242
    private val ownPid = 424243
    // SHA-256 of the exact UTF-8 record "424242\n", including its LF.
    private val claim = File(lock.path + ".takeover-92fb72daf8c68182")

    @After fun cleanUp() { directory.deleteRecursively() }

    @Test fun staleContenderCannotRemoveLiveSuccessorLock() {
        lock.writeText("$deadPid\n")
        val successorPid = 424244
        val staleRead = CountDownLatch(1)
        val successorEntered = CountDownLatch(1)
        val releaseSuccessor = CountDownLatch(1)
        val resumed = CompletableFuture<String>()
        val firstProbe = AtomicBoolean(true)
        val entries = AtomicInteger()
        val workers = Executors.newFixedThreadPool(2)
        try {
            // Only PID identity/liveness is a boundary fixture. Both contenders run
            // the real synchronous lock code and all lock/claim file operations.
            val stale = workers.submit<Unit> {
                LastKnownGood.withLock(file, "busy", ownPid, exited = { pid ->
                    when (pid) {
                        deadPid -> {
                            if (firstProbe.compareAndSet(true, false)) {
                                staleRead.countDown()
                                check(successorEntered.await(5, TimeUnit.SECONDS))
                            }
                            true
                        }
                        successorPid -> {
                            resumed.complete("observed live successor")
                            false
                        }
                        else -> false
                    }
                }) {
                    resumed.complete("entered before successor released")
                    entries.incrementAndGet()
                    assertEquals("$ownPid\n", lock.readText())
                }
            }
            assertTrue(staleRead.await(5, TimeUnit.SECONDS))
            val successor = workers.submit<Unit> {
                LastKnownGood.withLock(file, "busy", successorPid, exited = { it == deadPid }) {
                    successorEntered.countDown()
                    check(releaseSuccessor.await(5, TimeUnit.SECONDS))
                    assertEquals("$successorPid\n", lock.readText())
                }
            }
            assertTrue(successorEntered.await(5, TimeUnit.SECONDS))
            // This handshake proves A resumed after its stale observation and
            // inspected B's live record; lack of thread scheduling cannot pass.
            assertEquals("observed live successor", resumed.get(5, TimeUnit.SECONDS))
            assertEquals(0, entries.get())
            assertEquals("$successorPid\n", lock.readText())
            releaseSuccessor.countDown()
            successor.get(5, TimeUnit.SECONDS)
            stale.get(5, TimeUnit.SECONDS)
            assertEquals(1, entries.get())
            assertFalse(lock.exists())
            assertFalse(claim.exists())
        } finally {
            releaseSuccessor.countDown()
            workers.shutdownNow()
            assertTrue(workers.awaitTermination(5, TimeUnit.SECONDS))
        }
    }

    @Test fun deadHolderUsesCompatibleClaimAndReleasesAfterOperationFailure() {
        lock.writeText("$deadPid\n")
        var probes = 0
        val failure = IOException("operation failed")
        try {
            LastKnownGood.withLock<Unit>(file, "busy", ownPid, exited = { pid ->
                assertEquals(deadPid, pid)
                probes++
                if (probes == 2) {
                    assertEquals("$ownPid\n", claim.readText())
                    assertEquals(PosixFilePermissions.fromString("rw-------"), Files.getPosixFilePermissions(claim.toPath()))
                }
                true
            }) {
                assertEquals("$ownPid\n", lock.readText())
                assertFalse(claim.exists())
                throw failure
            }
            fail("must preserve the operation error")
        } catch (error: IOException) {
            assertSame(failure, error)
        }
        assertEquals(2, probes)
        assertFalse(lock.exists())
        assertFalse(claim.exists())
        assertEquals(42, LastKnownGood.withLock(file, "busy", ownPid, exited = { error("no holder") }) { 42 })
        assertFalse(lock.exists())
    }

    @Test fun existingTakeoverClaimIsNotReclaimed() {
        val record = "$deadPid\n"
        // Both an interrupted empty claim and a claim naming a dead process
        // require operator recovery; neither is a second stale-lock protocol.
        for (contents in listOf("", record)) {
            lock.writeText(record)
            claim.writeText(contents)
            expectBusy {
                LastKnownGood.withLock(file, "busy", ownPid, exited = { it == deadPid }) {
                    fail("an existing claim must prevent takeover")
                }
            }
            assertEquals(record, lock.readText())
            assertEquals(contents, claim.readText())
            assertTrue(claim.delete())
        }
    }

    @Test fun incompleteLockRecordIsRetainedWithoutProbingItsPid() {
        val incomplete = deadPid.toString() // No terminating LF: not a published PID record.
        lock.writeText(incomplete)
        expectBusy {
            LastKnownGood.withLock(file, "busy", ownPid, exited = { error("must not probe an incomplete record") }) {
                fail("an incomplete record must prevent acquisition")
            }
        }
        assertEquals(incomplete, lock.readText())
        assertFalse(claim.exists())
    }

    private fun expectBusy(action: () -> Unit) {
        try {
            action()
            fail("must time out without changing the existing lock")
        } catch (error: IllegalStateException) {
            assertEquals("busy", error.message)
        }
    }
}
