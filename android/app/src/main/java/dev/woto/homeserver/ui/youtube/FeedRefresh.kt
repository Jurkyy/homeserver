package dev.woto.homeserver.ui.youtube

/** Pure helpers for the YouTube feed's background auto-refresh (unit-tested). */
internal object FeedRefresh {
    /**
     * Ids the user hasn't seen yet after the feed went from [oldIds] to [newIds]: anything in the new list that wasn't
     * in the old one, plus still-present ids from an earlier unacknowledged update ([pending]).
     */
    fun pendingNewIds(oldIds: List<String>, newIds: List<String>, pending: Set<String> = emptySet()): Set<String> {
        val old = oldIds.toHashSet()
        val result = LinkedHashSet<String>()
        for (id in newIds) if (id !in old || id in pending) result += id
        return result
    }

    /**
     * Whether a server feed signal with content timestamp [signalAt] means our copy (content timestamp [loadedChangedAt])
     * is stale. [lastHandledAt] is the newest signal we already refetched for, so an old server that reports
     * `changed_at = 0` doesn't refetch repeatedly for the same signal.
     */
    fun shouldRefetch(signalAt: Long, loadedChangedAt: Long, lastHandledAt: Long): Boolean =
        signalAt > 0 && signalAt > maxOf(loadedChangedAt, lastHandledAt)
}
