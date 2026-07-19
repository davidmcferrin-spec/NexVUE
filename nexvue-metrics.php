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
 *   channel   optional — exact channel for "viewers" (e.g. ch0; alphanumeric)
 *
 *   Viewer column filters (view=viewers only; empty = no filter):
 *   filter_status    live/ended — plain text or /regex/flags
 *   filter_ip        stripped IP — plain text or /regex/flags
 *   filter_channel   channel name — plain text or /regex/flags (AND with channel=)
 *   filter_duration  duration — comparison (>=10m, <2h) or text/regex on display
 *   filter_data      bytes served — comparison (>500MB, <=1.5GB) or text/regex
 *   filter_client    user-agent — plain text or /regex/flags
 *
 *   Plain text is case-insensitive substring. Regex uses /pattern/flags (PCRE).
 *   Duration units: s/m/h (and sec/min/hr…). Data units: B/KB/MB/GB (SI, 1000).
 *   Comparisons: > >= < <= = against raw seconds / bytes.
 *
 * ---- Views -------------------------------------------------------------------
 *
 *   totals         System-wide time series: bandwidth, viewers, active streams.
 *   channels       Per-channel breakdown aggregated over the window.
 *   viewers        Per-viewer session drill-down (IP/channel/user/…).
 *   inputs         DeckLink input lock/format time series.
 *   weekday_hours  Mon–Sun × hour-of-day heatmap: equal-date averages
 *                  of bandwidth/viewers (missing telemetry excluded).
 *   host           Host CPU % / memory / load1 / CPU+GPU °C time series
 *                  (capacity analytics; not a CheckMK substitute).
 *
 * ---- Example calls -------------------------------------------------------------
 *
 *   nexvue-metrics.php?view=totals&range=1h
 *   nexvue-metrics.php?view=channels&from=1710000000&to=1710086400
 *   nexvue-metrics.php?view=weekday_hours&range=30d
 *   nexvue-metrics.php?view=host&range=24h
 *   nexvue-metrics.php?view=viewers&range=24h&filter_status=live&filter_duration=%3E%3D10m
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
const FILTER_MAX_LEN = 128;
const VIEWER_ACTIVE_WINDOW_S = 45;

function fail(int $status, string $message): never {
    http_response_code($status);
    echo json_encode(['error' => $message]);
    exit;
}

/** Display IP — same rule as metrics.html (first colon segment for IPv4:port). */
function viewer_display_ip(string $remoteAddr): string {
    $parts = explode(':', $remoteAddr, 2);
    return $parts[0];
}

function viewer_fmt_duration(float $seconds): string {
    if ($seconds >= 3600) {
        return sprintf('%.1fh', $seconds / 3600.0);
    }
    if ($seconds >= 60) {
        return (string)(int)round($seconds / 60.0) . 'm';
    }
    return (string)(int)round($seconds) . 's';
}

function viewer_fmt_data(int|float $bytes): string {
    return sprintf('%.1f MB', ((float)$bytes) / 1.0e6);
}

/**
 * Parse plain substring or /pattern/flags regex. Returns a matcher array.
 * @return array{type: string, needle?: string, pattern?: string}
 */
function parse_text_or_regex_filter(string $expr, string $label): array {
    if (strlen($expr) > FILTER_MAX_LEN) {
        fail(400, "{$label} filter must be at most " . FILTER_MAX_LEN . " characters");
    }
    if (preg_match('/^\/(.*)\/([imsxuADSUXJ]*)$/s', $expr, $m)) {
        $body = $m[1];
        $flags = $m[2];
        if ($body === '') {
            fail(400, "{$label} filter regex must not be empty");
        }
        if (strlen($body) > FILTER_MAX_LEN) {
            fail(400, "{$label} filter regex must be at most " . FILTER_MAX_LEN . " characters");
        }
        $pattern = '/' . $body . '/' . $flags;
        set_error_handler(static function () {});
        $ok = @preg_match($pattern, '');
        restore_error_handler();
        if ($ok === false) {
            fail(400, "{$label} filter: invalid regex");
        }
        return ['type' => 'regex', 'pattern' => $pattern];
    }
    return ['type' => 'substr', 'needle' => $expr];
}

function match_text_or_regex(string $haystack, array $filter): bool {
    if ($filter['type'] === 'regex') {
        $r = preg_match($filter['pattern'], $haystack);
        return $r === 1;
    }
    return stripos($haystack, $filter['needle']) !== false;
}

/**
 * Parse ">10m" / "<=1.5GB" style comparisons. null if not a comparison form.
 * @return array{op: string, value: float}|null
 */
function parse_compare_filter(string $expr, string $kind, string $label): ?array {
    if (!preg_match('/^(>=|<=|>|<|=)\s*(.+)$/s', $expr, $m)) {
        return null;
    }
    $op = $m[1];
    $rest = trim($m[2]);
    if ($rest === '') {
        fail(400, "{$label} filter comparison needs a value");
    }

    if ($kind === 'duration') {
        if (!preg_match('/^(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?$/u', $rest, $um)) {
            fail(400, "{$label} filter: expected number with optional unit (s/m/h)");
        }
        $n = (float)$um[1];
        $unit = strtolower($um[2] ?? 's');
        $mult = match ($unit) {
            '', 's', 'sec', 'secs', 'second', 'seconds' => 1.0,
            'm', 'min', 'mins', 'minute', 'minutes' => 60.0,
            'h', 'hr', 'hrs', 'hour', 'hours' => 3600.0,
            default => null,
        };
        if ($mult === null) {
            fail(400, "{$label} filter: unknown duration unit (use s, m, or h)");
        }
        return ['op' => $op, 'value' => $n * $mult];
    }

    if ($kind === 'data') {
        if (!preg_match('/^(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?$/u', $rest, $um)) {
            fail(400, "{$label} filter: expected number with optional unit (B/KB/MB/GB)");
        }
        $n = (float)$um[1];
        $unit = strtoupper($um[2] ?? 'B');
        $mult = match ($unit) {
            '', 'B' => 1.0,
            'KB', 'K' => 1.0e3,
            'MB', 'M' => 1.0e6,
            'GB', 'G' => 1.0e9,
            'TB', 'T' => 1.0e12,
            default => null,
        };
        if ($mult === null) {
            fail(400, "{$label} filter: unknown data unit (use B, KB, MB, or GB)");
        }
        return ['op' => $op, 'value' => $n * $mult];
    }

    fail(400, "{$label} filter: unsupported comparison kind");
}

function match_compare(float $actual, string $op, float $threshold): bool {
    return match ($op) {
        '>' => $actual > $threshold,
        '>=' => $actual >= $threshold,
        '<' => $actual < $threshold,
        '<=' => $actual <= $threshold,
        '=' => abs($actual - $threshold) < 1e-9,
        default => false,
    };
}

/**
 * Build compiled viewer filters from query params. Empty strings omitted.
 * @return array<string, array>
 */
function parse_viewer_filters(array $get): array {
    $keys = [
        'filter_status' => 'status',
        'filter_ip' => 'ip',
        'filter_channel' => 'channel',
        'filter_duration' => 'duration',
        'filter_data' => 'data',
        'filter_client' => 'client',
    ];
    $out = [];
    foreach ($keys as $param => $name) {
        if (!array_key_exists($param, $get)) {
            continue;
        }
        $raw = $get[$param];
        if (!is_string($raw) && !is_numeric($raw)) {
            fail(400, "{$name} filter must be a string");
        }
        $expr = trim((string)$raw);
        if ($expr === '') {
            continue;
        }
        if (strlen($expr) > FILTER_MAX_LEN) {
            fail(400, "{$name} filter must be at most " . FILTER_MAX_LEN . " characters");
        }

        if ($name === 'duration' || $name === 'data') {
            $cmp = parse_compare_filter($expr, $name === 'duration' ? 'duration' : 'data', $name);
            if ($cmp !== null) {
                $out[$name] = ['mode' => 'compare', 'op' => $cmp['op'], 'value' => $cmp['value'], 'expr' => $expr];
                continue;
            }
        }

        $text = parse_text_or_regex_filter($expr, $name);
        $out[$name] = ['mode' => 'text', 'filter' => $text, 'expr' => $expr];
    }
    return $out;
}

/** @param array<string, array> $filters */
function viewer_row_matches(array $row, array $filters): bool {
    foreach ($filters as $name => $spec) {
        if ($name === 'status') {
            $hay = !empty($row['active']) ? 'live' : 'ended';
            if ($spec['mode'] === 'text' && !match_text_or_regex($hay, $spec['filter'])) {
                return false;
            }
            continue;
        }
        if ($name === 'ip') {
            $hay = viewer_display_ip((string)($row['remote_addr'] ?? ''));
            if ($spec['mode'] === 'text' && !match_text_or_regex($hay, $spec['filter'])) {
                return false;
            }
            continue;
        }
        if ($name === 'channel') {
            $hay = (string)($row['channel'] ?? '');
            if ($spec['mode'] === 'text' && !match_text_or_regex($hay, $spec['filter'])) {
                return false;
            }
            continue;
        }
        if ($name === 'client') {
            $hay = (string)($row['user_agent'] ?? '');
            if ($spec['mode'] === 'text' && !match_text_or_regex($hay, $spec['filter'])) {
                return false;
            }
            continue;
        }
        if ($name === 'duration') {
            $secs = (float)($row['duration_s'] ?? 0);
            if ($spec['mode'] === 'compare') {
                if (!match_compare($secs, $spec['op'], (float)$spec['value'])) {
                    return false;
                }
            } else {
                $hay = viewer_fmt_duration($secs);
                if (!match_text_or_regex($hay, $spec['filter'])) {
                    return false;
                }
            }
            continue;
        }
        if ($name === 'data') {
            $bytes = (float)($row['bytes_sent'] ?? 0);
            if ($spec['mode'] === 'compare') {
                if (!match_compare($bytes, $spec['op'], (float)$spec['value'])) {
                    return false;
                }
            } else {
                $hay = viewer_fmt_data($bytes);
                if (!match_text_or_regex($hay, $spec['filter'])) {
                    return false;
                }
            }
            continue;
        }
    }
    return true;
}

/** Public filter exprs for JSON metadata (no compiled patterns). */
function viewer_filters_meta(array $filters): array {
    $meta = [];
    foreach ($filters as $name => $spec) {
        $meta[$name] = $spec['expr'];
    }
    return $meta;
}

function metrics_timezone(): DateTimeZone {
    // Reporting is Eastern Time. Override only if ops explicitly set
    // NEXVUE_METRICS_TZ (e.g. for a non-NY edge); blank/unset → America/New_York.
    $tzName = getenv('NEXVUE_METRICS_TZ');
    if (!is_string($tzName) || $tzName === '') {
        $tzName = 'America/New_York';
    }
    try {
        return new DateTimeZone($tzName);
    } catch (Exception $e) {
        fail(500, 'invalid NEXVUE_METRICS_TZ: ' . $tzName);
    }
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
    'timezone' => metrics_timezone()->getName(),
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
    $viewerFilters = parse_viewer_filters($_GET);

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
        $r['active'] = ($now - $r['last_seen']) < VIEWER_ACTIVE_WINDOW_S;
    }
    unset($r);

    $sessionTotal = count($rows);
    if ($viewerFilters !== []) {
        $rows = array_values(array_filter(
            $rows,
            static fn(array $r): bool => viewer_row_matches($r, $viewerFilters)
        ));
    }

    // Cast filters to object so empty encodes as {} not [] in JSON.
    echo json_encode($windowMeta + [
        'channel_filter' => $channelFilter,
        'filters' => (object)viewer_filters_meta($viewerFilters),
        'session_total' => $sessionTotal,
        'session_count' => count($rows),
        'sessions' => $rows,
    ]);
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

// ---- View: weekday_hours (Mon–Sun × hour analytical heatmap) ---------------------

if ($view === 'weekday_hours') {
    $tz = metrics_timezone();
    $tzName = $tz->getName();

    // Dense 7×24 grid (Mon=1 … Sun=7). Empty cells keep zeros.
    $cells = [];
    for ($dow = 1; $dow <= 7; $dow++) {
        for ($hour = 0; $hour <= 23; $hour++) {
            $cells["{$dow}_{$hour}"] = [
                'weekday' => $dow,
                'hour' => $hour,
                'avg_bandwidth_bps' => 0.0,
                'peak_bandwidth_bps' => 0.0,
                'avg_readers' => 0.0,
                'peak_readers' => 0,
                'sample_count' => 0,
                'date_count' => 0,
            ];
        }
    }

    $rows = queryAll($db,
        'SELECT ts, total_readers, total_bandwidth_bps
         FROM totals WHERE ts >= :since AND ts <= :until',
        $tsParams
    );

    // Two-stage aggregation in reporting TZ (America/New_York or NEXVUE_METRICS_TZ):
    // 1) average samples within each local calendar date + hour
    // 2) average those per-date means equally into weekday + hour
    // Missing telemetry (no samples that date/hour) is excluded, not zero-filled.
    $byDateHour = [];
    foreach ($rows as $r) {
        $dt = (new DateTimeImmutable('@' . (int)$r['ts']))->setTimezone($tz);
        $dow = (int)$dt->format('N'); // 1=Mon … 7=Sun
        $hour = (int)$dt->format('G');
        $date = $dt->format('Y-m-d');
        $dhKey = "{$date}_{$hour}";
        if (!isset($byDateHour[$dhKey])) {
            $byDateHour[$dhKey] = [
                'weekday' => $dow,
                'hour' => $hour,
                'sum_bw' => 0.0,
                'max_bw' => 0.0,
                'sum_r' => 0.0,
                'max_r' => 0,
                'n' => 0,
            ];
        }
        $bw = (float)$r['total_bandwidth_bps'];
        $rd = (int)$r['total_readers'];
        $byDateHour[$dhKey]['sum_bw'] += $bw;
        $byDateHour[$dhKey]['max_bw'] = max($byDateHour[$dhKey]['max_bw'], $bw);
        $byDateHour[$dhKey]['sum_r'] += $rd;
        $byDateHour[$dhKey]['max_r'] = max($byDateHour[$dhKey]['max_r'], $rd);
        $byDateHour[$dhKey]['n']++;
    }

    $acc = [];
    foreach ($byDateHour as $dh) {
        $n = $dh['n'];
        if ($n <= 0) {
            continue;
        }
        $key = "{$dh['weekday']}_{$dh['hour']}";
        if (!isset($acc[$key])) {
            $acc[$key] = [
                'sum_bw' => 0.0, 'max_bw' => 0.0,
                'sum_r' => 0.0, 'max_r' => 0,
                'sample_count' => 0, 'date_count' => 0,
            ];
        }
        $acc[$key]['sum_bw'] += $dh['sum_bw'] / $n;
        $acc[$key]['max_bw'] = max($acc[$key]['max_bw'], $dh['max_bw']);
        $acc[$key]['sum_r'] += $dh['sum_r'] / $n;
        $acc[$key]['max_r'] = max($acc[$key]['max_r'], $dh['max_r']);
        $acc[$key]['sample_count'] += $n;
        $acc[$key]['date_count']++;
    }

    foreach ($acc as $key => $a) {
        $dates = $a['date_count'];
        $cells[$key] = [
            'weekday' => (int)explode('_', $key)[0],
            'hour' => (int)explode('_', $key)[1],
            'avg_bandwidth_bps' => $dates ? $a['sum_bw'] / $dates : 0.0,
            'peak_bandwidth_bps' => $a['max_bw'],
            'avg_readers' => $dates ? $a['sum_r'] / $dates : 0.0,
            'peak_readers' => $a['max_r'],
            'sample_count' => $a['sample_count'],
            'date_count' => $dates,
        ];
    }

    $out = [];
    for ($dow = 1; $dow <= 7; $dow++) {
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

// ---- View: host (CPU / memory / load / temps) ------------------------------------

if ($view === 'host') {
    $stmt = $db->prepare(
        'SELECT ts, cpu_pct, mem_used_bytes, mem_total_bytes, load1,
                gpu_video_pct, gpu_render_pct, gpu_video_enhance_pct, gpu_freq_mhz,
                cpu_temp_c, gpu_temp_c
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
