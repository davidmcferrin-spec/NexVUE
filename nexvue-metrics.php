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
 *   view      one of: totals | channels | viewers | inputs | weekday_hours | host
 *   range     lookback: 15m | 1h | 6h | 24h | 7d | 30d  (default 1h; ignored if from+to set)
 *   from      optional Unix epoch (seconds) — custom window start (requires to)
 *   to        optional Unix epoch (seconds) — custom window end (requires from)
 *   channel   optional — restrict "viewers" to one channel (e.g. ch0)
 *
 * ---- Views -------------------------------------------------------------------
 *
 *   totals         System-wide time series: bandwidth, viewers, active streams.
 *   channels       Per-channel breakdown aggregated over the window.
 *   viewers        Per-viewer session drill-down (IP/channel/user/…).
 *   inputs         DeckLink input lock/format time series.
 *   weekday_hours  Mon–Fri × hour-of-day heatmap buckets from totals.
 *   host           Host CPU % / memory / load1 time series (capacity analytics;
 *                  not a CheckMK substitute).
 *
 * ---- Example calls -------------------------------------------------------------
 *
 *   nexvue-metrics.php?view=totals&range=1h
 *   nexvue-metrics.php?view=channels&from=1710000000&to=1710086400
 *   nexvue-metrics.php?view=weekday_hours&range=30d
 *   nexvue-metrics.php?view=host&range=24h
 */

declare(strict_types=1);

header('Content-Type: application/json');
header('Cache-Control: no-store');

$DB_PATH = getenv('NEXVUE_METRICS_DB') ?: '/var/lib/nexvue/metrics.db';

const VALID_RANGES = [
    '15m' => 15 * 60,
    '1h'  => 60 * 60,
    '6h'  => 6 * 60 * 60,
    '24h' => 24 * 60 * 60,
    '7d'  => 7 * 24 * 60 * 60,
    '30d' => 30 * 24 * 60 * 60,
];

const MAX_WINDOW_S = 30 * 24 * 60 * 60;

function fail(int $status, string $message): never {
    http_response_code($status);
    echo json_encode(['error' => $message]);
    exit;
}

function metrics_timezone(): DateTimeZone {
    $tzName = getenv('NEXVUE_METRICS_TZ');
    if (is_string($tzName) && $tzName !== '') {
        try {
            return new DateTimeZone($tzName);
        } catch (Exception $e) {
            fail(500, 'invalid NEXVUE_METRICS_TZ: ' . $tzName);
        }
    }
    return new DateTimeZone(date_default_timezone_get());
}

// ---- Parse & validate query params ---------------------------------------------

$view = $_GET['view'] ?? '';
$validViews = ['totals', 'channels', 'viewers', 'inputs', 'weekday_hours', 'host'];
if (!in_array($view, $validViews, true)) {
    fail(400, 'view must be one of: ' . implode(', ', $validViews));
}

$fromRaw = $_GET['from'] ?? null;
$toRaw = $_GET['to'] ?? null;
$rangeKey = $_GET['range'] ?? '1h';
$now = time();

if ($fromRaw !== null || $toRaw !== null) {
    if ($fromRaw === null || $toRaw === null) {
        fail(400, 'from and to must both be set for a custom window');
    }
    if (!ctype_digit((string)$fromRaw) || !ctype_digit((string)$toRaw)) {
        fail(400, 'from and to must be Unix epoch integers (seconds)');
    }
    $sinceTs = (int)$fromRaw;
    $untilTs = (int)$toRaw;
    if ($sinceTs >= $untilTs) {
        fail(400, 'from must be earlier than to');
    }
    if (($untilTs - $sinceTs) > MAX_WINDOW_S) {
        fail(400, 'custom window must be at most 30 days');
    }
    $rangeKey = 'custom';
} else {
    if (!array_key_exists($rangeKey, VALID_RANGES)) {
        fail(400, 'range must be one of: ' . implode(', ', array_keys(VALID_RANGES)));
    }
    $sinceTs = $now - VALID_RANGES[$rangeKey];
    $untilTs = $now;
}

$channelFilter = $_GET['channel'] ?? null;
if ($channelFilter !== null && !preg_match('/^[a-zA-Z0-9]+$/', $channelFilter)) {
    fail(400, 'channel must be alphanumeric');
}

$windowMeta = [
    'range' => $rangeKey,
    'from' => $sinceTs,
    'to' => $untilTs,
];

// ---- Open the database, READ-ONLY -------------------------------------------------

if (!is_readable($DB_PATH)) {
    fail(503, "metrics database not readable at $DB_PATH — is nexvue-metrics.service running?");
}

try {
    $db = new SQLite3($DB_PATH, SQLITE3_OPEN_READONLY);
    $db->busyTimeout(3000);
} catch (Exception $e) {
    fail(503, 'could not open metrics database: ' . $e->getMessage());
}

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

$tsParams = [':since' => $sinceTs, ':until' => $untilTs];

// ---- View: totals ----------------------------------------------------------------

if ($view === 'totals') {
    $rows = queryAll($db,
        'SELECT ts, active_streams, total_readers, total_bandwidth_bps
         FROM totals WHERE ts >= :since AND ts <= :until ORDER BY ts ASC',
        $tsParams
    );
    echo json_encode($windowMeta + ['totals' => $rows]);
    exit;
}

// ---- View: channels --------------------------------------------------------------

if ($view === 'channels') {
    $rows = queryAll($db,
        'SELECT channel,
                AVG(bandwidth_bps)                                  AS avg_bandwidth_bps,
                MAX(bandwidth_bps)                                  AS peak_bandwidth_bps,
                AVG(readers)                                        AS avg_readers,
                MAX(readers)                                        AS peak_readers,
                ROUND(100.0 * SUM(ready) / COUNT(*), 1)             AS ready_pct,
                COUNT(*)                                            AS sample_count
         FROM samples
         WHERE ts >= :since AND ts <= :until
         GROUP BY channel
         ORDER BY avg_bandwidth_bps DESC',
        $tsParams
    );
    echo json_encode($windowMeta + ['channels' => $rows]);
    exit;
}

// ---- View: viewers ---------------------------------------------------------------

if ($view === 'viewers') {
    $sql = 'SELECT session_id, remote_addr, channel, user, user_agent,
                   first_seen, last_seen, bytes_sent
            FROM viewer_sessions
            WHERE last_seen >= :since AND last_seen <= :until';
    $params = $tsParams;
    if ($channelFilter !== null) {
        $sql .= ' AND channel = :channel';
        $params[':channel'] = $channelFilter;
    }
    $sql .= ' ORDER BY last_seen DESC';

    $rows = queryAll($db, $sql, $params);
    foreach ($rows as &$r) {
        $r['duration_s'] = round($r['last_seen'] - $r['first_seen'], 1);
        $r['active'] = ($now - $r['last_seen']) < 45;
    }
    unset($r);

    echo json_encode($windowMeta + ['channel_filter' => $channelFilter, 'sessions' => $rows]);
    exit;
}

// ---- View: inputs ----------------------------------------------------------------

if ($view === 'inputs') {
    $rows = queryAll($db,
        'SELECT ts, device_index, card_name, input_locked, input_mode,
                reference_locked, reference_mode
         FROM input_status WHERE ts >= :since AND ts <= :until ORDER BY ts ASC',
        $tsParams
    );
    echo json_encode($windowMeta + ['inputs' => $rows]);
    exit;
}

// ---- View: weekday_hours (Mon–Fri × hour heatmap) --------------------------------

if ($view === 'weekday_hours') {
    $tz = metrics_timezone();
    $tzName = $tz->getName();

    // Dense 5×24 grid (Mon=1 … Fri=5). Empty cells keep zeros.
    $cells = [];
    for ($dow = 1; $dow <= 5; $dow++) {
        for ($hour = 0; $hour <= 23; $hour++) {
            $cells["{$dow}_{$hour}"] = [
                'weekday' => $dow,
                'hour' => $hour,
                'avg_bandwidth_bps' => 0.0,
                'peak_bandwidth_bps' => 0.0,
                'avg_readers' => 0.0,
                'peak_readers' => 0,
                'sample_count' => 0,
            ];
        }
    }

    $rows = queryAll($db,
        'SELECT ts, total_readers, total_bandwidth_bps
         FROM totals WHERE ts >= :since AND ts <= :until',
        $tsParams
    );

    // Accumulate in PHP so weekday/hour use NEXVUE_METRICS_TZ (or PHP default),
    // not the SQLite connection's UTC assumption.
    $acc = [];
    foreach ($rows as $r) {
        $dt = (new DateTimeImmutable('@' . (int)$r['ts']))->setTimezone($tz);
        $dow = (int)$dt->format('N'); // 1=Mon … 7=Sun
        if ($dow > 5) {
            continue;
        }
        $hour = (int)$dt->format('G');
        $key = "{$dow}_{$hour}";
        if (!isset($acc[$key])) {
            $acc[$key] = [
                'sum_bw' => 0.0, 'max_bw' => 0.0,
                'sum_r' => 0.0, 'max_r' => 0, 'n' => 0,
            ];
        }
        $bw = (float)$r['total_bandwidth_bps'];
        $rd = (int)$r['total_readers'];
        $acc[$key]['sum_bw'] += $bw;
        $acc[$key]['max_bw'] = max($acc[$key]['max_bw'], $bw);
        $acc[$key]['sum_r'] += $rd;
        $acc[$key]['max_r'] = max($acc[$key]['max_r'], $rd);
        $acc[$key]['n']++;
    }

    foreach ($acc as $key => $a) {
        $n = $a['n'];
        $cells[$key] = [
            'weekday' => (int)explode('_', $key)[0],
            'hour' => (int)explode('_', $key)[1],
            'avg_bandwidth_bps' => $n ? $a['sum_bw'] / $n : 0.0,
            'peak_bandwidth_bps' => $a['max_bw'],
            'avg_readers' => $n ? $a['sum_r'] / $n : 0.0,
            'peak_readers' => $a['max_r'],
            'sample_count' => $n,
        ];
    }

    $out = [];
    for ($dow = 1; $dow <= 5; $dow++) {
        for ($hour = 0; $hour <= 23; $hour++) {
            $out[] = $cells["{$dow}_{$hour}"];
        }
    }

    echo json_encode($windowMeta + [
        'timezone' => $tzName,
        'weekday_hours' => $out,
    ]);
    exit;
}

// ---- View: host (CPU / memory / load) --------------------------------------------

if ($view === 'host') {
    $stmt = $db->prepare(
        'SELECT ts, cpu_pct, mem_used_bytes, mem_total_bytes, load1,
                gpu_video_pct, gpu_render_pct, gpu_video_enhance_pct, gpu_freq_mhz
         FROM host_samples WHERE ts >= :since AND ts <= :until ORDER BY ts ASC'
    );
    if ($stmt === false) {
        // Table missing until nexvue-metrics is restarted after upgrade.
        echo json_encode($windowMeta + ['host' => []]);
        exit;
    }
    $stmt->bindValue(':since', $sinceTs);
    $stmt->bindValue(':until', $untilTs);
    $result = $stmt->execute();
    $rows = [];
    while ($row = $result->fetchArray(SQLITE3_ASSOC)) {
        $rows[] = $row;
    }
    echo json_encode($windowMeta + ['host' => $rows]);
    exit;
}

fail(400, 'unknown view');
