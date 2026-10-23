package dev.woto.homeserver.ui.youtube

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class FeedRefreshTest {

    @Test
    fun pendingNewIdsCountsIdsMissingFromOldList() {
        val ids = FeedRefresh.pendingNewIds(listOf("a", "b", "c"), listOf("x", "y", "a", "b"))
        assertEquals(setOf("x", "y"), ids)
    }

    @Test
    fun pendingNewIdsIsEmptyWhenOnlyReorderedOrRemoved() {
        assertTrue(FeedRefresh.pendingNewIds(listOf("a", "b", "c"), listOf("c", "a")).isEmpty())
    }

    @Test
    fun pendingNewIdsAccumulatesUnacknowledgedAndDropsRemoved() {
        val first = FeedRefresh.pendingNewIds(listOf("a"), listOf("x", "y", "a"))
        val second = FeedRefresh.pendingNewIds(listOf("x", "y", "a"), listOf("z", "x", "a"), first)
        assertEquals(setOf("z", "x"), second) // y vanished from the feed
    }

    @Test
    fun shouldRefetchOnlyWhenSignalIsNewer() {
        assertTrue(FeedRefresh.shouldRefetch(signalAt = 200, loadedChangedAt = 100, lastHandledAt = 0))
        assertFalse(FeedRefresh.shouldRefetch(signalAt = 100, loadedChangedAt = 100, lastHandledAt = 0))
        assertFalse(FeedRefresh.shouldRefetch(signalAt = 50, loadedChangedAt = 100, lastHandledAt = 0))
        assertFalse(FeedRefresh.shouldRefetch(signalAt = 0, loadedChangedAt = 0, lastHandledAt = 0))
        // old server without changed_at: refetch once per signal, not on every re-send
        assertTrue(FeedRefresh.shouldRefetch(signalAt = 300, loadedChangedAt = 0, lastHandledAt = 0))
        assertFalse(FeedRefresh.shouldRefetch(signalAt = 300, loadedChangedAt = 0, lastHandledAt = 300))
    }
}
