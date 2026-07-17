<?php
/**
 * nexvue-metrics.php — reads NexVUE's usage/analytics SQLite database
 * directly and serves JSON. No Python HTTP server, no reverse proxy, no
 * WebSocket, no new port — just Apache running PHP against a database file
 * on the same disk, which is about as "Apache-native" as a data source gets.
 *
 * The collector (nexvue-metrics-server.py) only ever WRITES to this
 * database; this script only ever READS from it (opened SQLITE3_OPEN_READONLY
 * below), so there's no write-contention risk between the two processes
 * beyond what SQLite's WAL mode already handles natively.
 *
 * ---- Query parameters -------------------------------------------------------
 *
 *   view      one of: totals | channels | viewers | inputs   (required)
 *   range     lookback window: 15m | 1h | 6h | 24h | 7d | 30d  (default 1h)
 *   channel   optional — restrict "viewers" to one channel (e.g. ch0)
 *
 * ---- Views -------------------------------------------------------------------
 *
 *   totals    System-wide time series: bandwidth, viewer count, active
 *             stream count, one row per poll cycle in range. Matches the
 *             dashboard's three top-line charts.
 *
 *   channels  Per-channel BREAKDOWN over the range: average bandwidth,
 *             average viewers, and time-in-ready(%) for EACH channel —
 *             "how much bandwidth did ch0 use in the last hour" as a single
 *             row per channel, not a time series.
 *
 *   viewers   Per-viewer session drill-down: IP, channel, user (once Phase 2
 *             auth exists), first/last seen, duration, bytes served,
 *             live/ended status. Optionally filtered to one channel.
 *
 *   inputs    Per-DeckLink-input lock/format history as a time series —
 *             matches the dashboard's input-lock chart.
 *
 * ---- Example calls -------------------------------------------------------------
 *
 *   nexvue-metrics.php?view=totals&range=1h
 *   nexvue-metrics.php?view=channels&range=24h        (system broken down by channel)
 *   nexvue-metrics.php?view=viewers&range=15m&channel=ch0
 *   nexvue-metrics.php?view=inputs&range=6h
 */

declare(strict_types=1);

header('Content-Type: application/json');
header('Cache-Control: no-store');

// ---- Configuration ------------------------------------------------------------
// Same DB the Python collector writes to. Override via a Apache/PHP-FPM env
// var (SetEnv NEXVUE_METRICS_DB /path/to/metrics.db in the vhost) if it
// isn't at the default location.
$DB_PATH = getenv('NEXVUE_METRICS_DB') ?: '/var/lib/nexvue/metrics.db';

const VALID_RANGES = [
    '15m' => 15 * 60,
    '1h'  => 60 * 60,
    '6h'  => 6 * 60 * 60,
    '24h' => 24 * 60 * 60,
    '7d'  => 7 * 24 * 60 * 60,
    '30d' => 30 * 24 * 60 * 60,
];

function fail(int $status, string $message): never {
    http_response_code($status);
    echo json_encode(['error' => $message]);
    exit;
}

// ---- Parse & validate query params ---------------------------------------------

$view = $_GET['view'] ?? '';
if (!in_array($view, ['totals', 'channels', 'viewers', 'inputs'], true)) {
    fail(400, "view must be one of: totals, channels, viewers, inputs");
}

$rangeKey = $_GET['range'] ?? '1h';
if (!array_key_exists($rangeKey, VALID_RANGES)) {
    fail(400, 'range must be one of: ' . implode(', ', array_keys(VALID_RANGES)));
}
$sinceTs = time() - VALID_RANGES[$rangeKey];

// Channel filter: alphanumeric only (matches NexVUE's chN/chNlo naming) —
// rejected outright rather than escaped, since there's no legitimate reason
// for it to contain anything else and this keeps the SQL trivially safe.
$channelFilter = $_GET['channel'] ?? null;
if ($channelFilter !== null && !preg_match('/^[a-zA-Z0-9]+$/', $channelFilter)) {
    fail(400, 'channel must be alphanumeric');
}

// ---- Open the database, READ-ONLY -------------------------------------------------
// SQLITE3_OPEN_READONLY means this script can never corrupt or block the
// collector's writes, and needs no write permission on the file or its
// containing directory (only read+execute-to-traverse).

if (!is_readable($DB_PATH)) {
    fail(503, "metrics database not readable at $DB_PATH — is nexvue-metrics.service running?");
}

try {
    $db = new SQLite3($DB_PATH, SQLITE3_OPEN_READONLY);
    $db->busyTimeout(3000);
} catch (Exception $e) {
    fail(503, 'could not open metrics database: ' . $e->getMessage());
}

// ---- Helpers --------------------------------------------------------------------

function queryAll(SQLite3 $db, string $sql, array $params = []): array {
    $stmt = $db->prepare($sql);
    foreach ($params as $key => $value) {
        $stmt->bindValue($key, $value);
    }
    $result = $stmt->execute();
    $rows = [];
    while ($row = $result->fetchArray(SQLITE3_ASSOC)) {
        $rows[] = $row;
    }
    return $rows;
}

// ---- View: totals (system-wide time series) --------------------------------------

if ($view === 'totals') {
    $rows = queryAll($db,
        'SELECT ts, active_streams, total_readers, total_bandwidth_bps
         FROM totals WHERE ts >= :since ORDER BY ts ASC',
        [':since' => $sinceTs]
    );
    echo json_encode(['range' => $rangeKey, 'totals' => $rows]);
    exit;
}

// ---- View: channels (per-channel breakdown, aggregated over the range) -----------

if ($view === 'channels') {
    // One row per channel: averages over the window, plus % of samples the
    // channel was actually "ready" (publishing) — a channel that only came
    // up partway through the range will show a lower ready% accordingly.
    $rows = queryAll($db,
        'SELECT channel,
                AVG(bandwidth_bps)                                  AS avg_bandwidth_bps,
                MAX(bandwidth_bps)                                  AS peak_bandwidth_bps,
                AVG(readers)                                        AS avg_readers,
                MAX(readers)                                        AS peak_readers,
                ROUND(100.0 * SUM(ready) / COUNT(*), 1)             AS ready_pct,
                COUNT(*)                                            AS sample_count
         FROM samples
         WHERE ts >= :since
         GROUP BY channel
         ORDER BY avg_bandwidth_bps DESC',
        [':since' => $sinceTs]
    );
    echo json_encode(['range' => $rangeKey, 'channels' => $rows]);
    exit;
}

// ---- View: viewers (per-session IP/channel drill-down) ---------------------------

if ($view === 'viewers') {
    $sql = 'SELECT session_id, remote_addr, channel, user, user_agent,
                   first_seen, last_seen, bytes_sent
            FROM viewer_sessions
            WHERE last_seen >= :since';
    $params = [':since' => $sinceTs];
    if ($channelFilter !== null) {
        $sql .= ' AND channel = :channel';
        $params[':channel'] = $channelFilter;
    }
    $sql .= ' ORDER BY last_seen DESC';

    $rows = queryAll($db, $sql, $params);
    $now = time();
    foreach ($rows as &$r) {
        $r['duration_s'] = round($r['last_seen'] - $r['first_seen'], 1);
        $r['active'] = ($now - $r['last_seen']) < 45;  // ~3x the collector's poll interval
    }
    unset($r);

    echo json_encode(['range' => $rangeKey, 'channel_filter' => $channelFilter, 'sessions' => $rows]);
    exit;
}

// ---- View: inputs (DeckLink lock/format time series) -----------------------------

if ($view === 'inputs') {
    $rows = queryAll($db,
        'SELECT ts, device_index, card_name, input_locked, input_mode,
                reference_locked, reference_mode
         FROM input_status WHERE ts >= :since ORDER BY ts ASC',
        [':since' => $sinceTs]
    );
    echo json_encode(['range' => $rangeKey, 'inputs' => $rows]);
    exit;
}
