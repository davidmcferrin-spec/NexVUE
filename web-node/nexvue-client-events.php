<?php
/**
 * nexvue-client-events.php — opt-in Player / Multiview session reports.
 *
 * Separate SQLite from metrics.db: Apache (www-data) writes here; the
 * nexvue-metrics collector never touches this file. Nothing is stored
 * until a viewer clicks Report session.
 *
 * POST  (any logged-in user or share)  ingest snapshot / event
 * GET   action=list  (admin/operator)  reports in a time window
 * GET   action=get   (admin/operator)  one session's snapshots + events
 */

declare(strict_types=1);

require_once __DIR__ . '/nexvue-auth-lib.php';

header('Content-Type: application/json');
header('Cache-Control: no-store');

const CLIENT_EVENTS_MAX_BODY = 8192;
const CLIENT_EVENTS_UA_MAX = 240;
const CLIENT_EVENTS_DETAIL_MAX = 400;
const CLIENT_EVENTS_LABEL_MAX = 160;
const CLIENT_EVENTS_SNAP_MIN_S = 15;
const CLIENT_EVENTS_MAX_SNAPS = 400;
const CLIENT_EVENTS_MAX_EVENTS = 300;
const CLIENT_EVENTS_RETENTION_S = 7 * 24 * 3600;
const CLIENT_EVENTS_VALID_PAGES = ['player', 'multiview'];
const CLIENT_EVENTS_VALID_EVENTS = [
    'start', 'stop', 'channel', 'rendition', 'whep_error', 'ice_failed',
    'kicked', 'hidden', 'visible', 'pagehide', 'reconnect',
];
const CLIENT_EVENTS_RANGES = [
    '15m' => 15 * 60,
    '1h'  => 60 * 60,
    '6h'  => 6 * 60 * 60,
    '24h' => 24 * 60 * 60,
    '7d'  => 7 * 24 * 60 * 60,
    '30d' => 30 * 24 * 60 * 60,
];

function ce_fail(int $status, string $message): never {
    http_response_code($status);
    echo json_encode(['ok' => false, 'error' => $message]);
    exit;
}

function ce_ok(array $payload): never {
    echo json_encode(['ok' => true] + $payload);
    exit;
}

function ce_session_id_ok(string $id): bool {
    if (preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $id)) {
        return true;
    }
    return (bool)preg_match('/^c[0-9a-f]{32}$/i', $id);
}

function ce_db_path(): string {
    $env = getenv('NEXVUE_CLIENT_EVENTS_DB');
    if (is_string($env) && $env !== '') {
        return $env;
    }
    return '/var/lib/nexvue/client-events/client-events.db';
}

function ce_db(): SQLite3 {
    static $db = null;
    if ($db instanceof SQLite3) {
        return $db;
    }
    $path = ce_db_path();
    $dir = dirname($path);
    if (!is_dir($dir)) {
        if (!@mkdir($dir, 0750, true) && !is_dir($dir)) {
            throw new RuntimeException('client-events dir missing');
        }
    }
    $db = new SQLite3($path);
    $db->busyTimeout(3000);
    $db->exec('PRAGMA journal_mode=WAL');
    $db->exec('PRAGMA synchronous=NORMAL');
    $db->exec(<<<'SQL'
CREATE TABLE IF NOT EXISTS reports (
    session_id TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    channel TEXT,
    page TEXT,
    username TEXT,
    client_label TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    inbound_bps REAL,
    loss_pct REAL,
    rtt_ms REAL,
    jb_ms REAL,
    width INTEGER,
    height INTEGER,
    fps REAL,
    frames_dropped INTEGER,
    rendition TEXT,
    channel TEXT,
    hidden INTEGER,
    hw_concurrency INTEGER,
    net_type TEXT,
    ua TEXT
);
CREATE INDEX IF NOT EXISTS idx_ce_snap_sid_ts ON snapshots(session_id, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ce_evt_sid_ts ON events(session_id, ts);
SQL);
    return $db;
}

function ce_prune(SQLite3 $db, int $now): void {
    $cut = $now - CLIENT_EVENTS_RETENTION_S;
    $db->exec('DELETE FROM snapshots WHERE ts < ' . $cut);
    $db->exec('DELETE FROM events WHERE ts < ' . $cut);
    $db->exec('DELETE FROM reports WHERE last_seen < ' . $cut);
}

function ce_channel_ok(?string $channel): bool {
    if ($channel === null || $channel === '') {
        return true;
    }
    return (bool)preg_match('/^[a-zA-Z0-9]{1,16}$/', $channel);
}

function ce_channel_allowed(array $me, ?string $channel): bool {
    if ($channel === null || $channel === '') {
        return true;
    }
    $acl = $me['channels'] ?? null;
    if (!is_array($acl) || $acl === []) {
        return true;
    }
    $want = strtolower($channel);
    $base = preg_replace('/lo$/', '', $want);
    foreach ($acl as $c) {
        $have = strtolower((string)$c);
        if ($have === $want || $have === $base || $have . 'lo' === $want) {
            return true;
        }
    }
    return false;
}

function ce_clip(?string $s, int $max): ?string {
    if ($s === null) {
        return null;
    }
    $s = trim($s);
    if ($s === '') {
        return null;
    }
    if (strlen($s) > $max) {
        return substr($s, 0, $max);
    }
    return $s;
}

function ce_num($v): ?float {
    if ($v === null || $v === '') {
        return null;
    }
    if (!is_numeric($v)) {
        return null;
    }
    $n = (float)$v;
    if (!is_finite($n)) {
        return null;
    }
    return $n;
}

function ce_int($v): ?int {
    $n = ce_num($v);
    return $n === null ? null : (int)round($n);
}

function ce_window(): array {
    $fromRaw = $_GET['from'] ?? null;
    $toRaw = $_GET['to'] ?? null;
    $rangeKey = (string)($_GET['range'] ?? '24h');
    $now = time();
    if ($fromRaw !== null || $toRaw !== null) {
        if ($fromRaw === null || $toRaw === null) {
            ce_fail(400, 'from and to must both be set');
        }
        if (!ctype_digit((string)$fromRaw) || !ctype_digit((string)$toRaw)) {
            ce_fail(400, 'from and to must be Unix epoch integers');
        }
        $from = (int)$fromRaw;
        $to = (int)$toRaw;
        if ($from >= $to) {
            ce_fail(400, 'from must be earlier than to');
        }
        if (($to - $from) > 30 * 24 * 3600) {
            ce_fail(400, 'window must be at most 30 days');
        }
        return [$from, $to];
    }
    if (!array_key_exists($rangeKey, CLIENT_EVENTS_RANGES)) {
        ce_fail(400, 'range must be one of: ' . implode(', ', array_keys(CLIENT_EVENTS_RANGES)));
    }
    return [$now - CLIENT_EVENTS_RANGES[$rangeKey], $now];
}

function ce_read_json_body(): array {
    $envBody = getenv('NEXVUE_CLIENT_EVENTS_BODY');
    $raw = (is_string($envBody) && $envBody !== '')
        ? $envBody
        : file_get_contents('php://input');
    if (!is_string($raw) || $raw === '') {
        return [];
    }
    if (strlen($raw) > CLIENT_EVENTS_MAX_BODY) {
        ce_fail(413, 'report payload too large');
    }
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        ce_fail(400, 'report body must be JSON');
    }
    return $data;
}

function ce_upsert_report(SQLite3 $db, string $sid, int $now, array $me, array $body): void {
    $channel = ce_clip(isset($body['channel']) ? (string)$body['channel'] : null, 16);
    $page = isset($body['page']) ? (string)$body['page'] : null;
    if ($page !== null && !in_array($page, CLIENT_EVENTS_VALID_PAGES, true)) {
        $page = null;
    }
    $user = ce_clip(isset($me['username']) ? (string)$me['username'] : null, 64);
    if ($user === null && isset($me['auth']) && $me['auth'] === 'share') {
        $user = 'share';
    }
    $label = ce_clip(isset($body['client_label']) ? (string)$body['client_label'] : null, CLIENT_EVENTS_LABEL_MAX);
    $st = $db->prepare(
        'INSERT INTO reports (session_id, first_seen, last_seen, channel, page, username, client_label)
         VALUES (:sid, :now, :now, :ch, :page, :user, :label)
         ON CONFLICT(session_id) DO UPDATE SET
           last_seen=excluded.last_seen,
           channel=COALESCE(excluded.channel, reports.channel),
           page=COALESCE(excluded.page, reports.page),
           username=COALESCE(excluded.username, reports.username),
           client_label=COALESCE(excluded.client_label, reports.client_label)'
    );
    $st->bindValue(':sid', $sid, SQLITE3_TEXT);
    $st->bindValue(':now', $now, SQLITE3_INTEGER);
    $st->bindValue(':ch', $channel);
    $st->bindValue(':page', $page);
    $st->bindValue(':user', $user);
    $st->bindValue(':label', $label);
    $st->execute();
}

function ce_count(SQLite3 $db, string $table, string $sid): int {
    $st = $db->prepare("SELECT COUNT(*) AS n FROM {$table} WHERE session_id = :sid");
    $st->bindValue(':sid', $sid, SQLITE3_TEXT);
    $row = $st->execute()->fetchArray(SQLITE3_ASSOC);
    return (int)($row['n'] ?? 0);
}

function ce_ingest(SQLite3 $db, array $me, array $body): array {
    $sid = isset($body['session_id']) ? trim((string)$body['session_id']) : '';
    if (!ce_session_id_ok($sid)) {
        ce_fail(400, 'session_id must be a WHEP UUID or client report key');
    }
    $channel = isset($body['channel']) ? trim((string)$body['channel']) : '';
    if ($channel !== '' && !ce_channel_ok($channel)) {
        ce_fail(400, 'channel must be alphanumeric');
    }
    if ($channel !== '' && !ce_channel_allowed($me, $channel)) {
        ce_fail(403, 'channel not allowed');
    }
    $kind = isset($body['kind']) ? (string)$body['kind'] : '';
    if ($kind !== 'snapshot' && $kind !== 'event') {
        ce_fail(400, 'kind must be snapshot or event');
    }

    $now = time();
    ce_prune($db, $now);
    ce_upsert_report($db, $sid, $now, $me, $body);

    if ($kind === 'event') {
        $ev = isset($body['event']) ? (string)$body['event'] : '';
        if (!in_array($ev, CLIENT_EVENTS_VALID_EVENTS, true)) {
            ce_fail(400, 'unknown event');
        }
        if (ce_count($db, 'events', $sid) >= CLIENT_EVENTS_MAX_EVENTS) {
            return ['skipped' => 'event_cap'];
        }
        $detail = ce_clip(isset($body['detail']) ? (string)$body['detail'] : null, CLIENT_EVENTS_DETAIL_MAX);
        $st = $db->prepare(
            'INSERT INTO events (session_id, ts, kind, detail) VALUES (:sid, :ts, :kind, :detail)'
        );
        $st->bindValue(':sid', $sid, SQLITE3_TEXT);
        $st->bindValue(':ts', $now, SQLITE3_INTEGER);
        $st->bindValue(':kind', $ev, SQLITE3_TEXT);
        $st->bindValue(':detail', $detail);
        $st->execute();
        return ['stored' => 'event'];
    }

    $last = $db->prepare('SELECT MAX(ts) AS ts FROM snapshots WHERE session_id = :sid');
    $last->bindValue(':sid', $sid, SQLITE3_TEXT);
    $prev = $last->execute()->fetchArray(SQLITE3_ASSOC);
    $prevTs = (int)($prev['ts'] ?? 0);
    if ($prevTs > 0 && ($now - $prevTs) < CLIENT_EVENTS_SNAP_MIN_S) {
        return ['skipped' => 'rate'];
    }
    if (ce_count($db, 'snapshots', $sid) >= CLIENT_EVENTS_MAX_SNAPS) {
        return ['skipped' => 'snapshot_cap'];
    }

    $snap = isset($body['snapshot']) && is_array($body['snapshot']) ? $body['snapshot'] : [];
    $st = $db->prepare(
        'INSERT INTO snapshots (
            session_id, ts, inbound_bps, loss_pct, rtt_ms, jb_ms,
            width, height, fps, frames_dropped, rendition, channel,
            hidden, hw_concurrency, net_type, ua
         ) VALUES (
            :sid, :ts, :bps, :loss, :rtt, :jb,
            :w, :h, :fps, :drop, :rend, :ch,
            :hidden, :hw, :net, :ua
         )'
    );
    $st->bindValue(':sid', $sid, SQLITE3_TEXT);
    $st->bindValue(':ts', $now, SQLITE3_INTEGER);
    $st->bindValue(':bps', ce_num($snap['inbound_bps'] ?? null));
    $st->bindValue(':loss', ce_num($snap['loss_pct'] ?? null));
    $st->bindValue(':rtt', ce_num($snap['rtt_ms'] ?? null));
    $st->bindValue(':jb', ce_num($snap['jb_ms'] ?? null));
    $st->bindValue(':w', ce_int($snap['width'] ?? null));
    $st->bindValue(':h', ce_int($snap['height'] ?? null));
    $st->bindValue(':fps', ce_num($snap['fps'] ?? null));
    $st->bindValue(':drop', ce_int($snap['frames_dropped'] ?? null));
    $st->bindValue(':rend', ce_clip(isset($snap['rendition']) ? (string)$snap['rendition'] : null, 8));
    $st->bindValue(':ch', ce_clip($channel !== '' ? $channel : (isset($snap['channel']) ? (string)$snap['channel'] : null), 16));
    $hidden = $snap['hidden'] ?? null;
    $st->bindValue(':hidden', $hidden === null ? null : ((int)(bool)$hidden));
    $st->bindValue(':hw', ce_int($snap['hw_concurrency'] ?? null));
    $st->bindValue(':net', ce_clip(isset($snap['net_type']) ? (string)$snap['net_type'] : null, 24));
    $st->bindValue(':ua', ce_clip(isset($snap['ua']) ? (string)$snap['ua'] : null, CLIENT_EVENTS_UA_MAX));
    $st->execute();
    return ['stored' => 'snapshot'];
}

function ce_list(SQLite3 $db, int $from, int $to): array {
    $st = $db->prepare(
        'SELECT session_id, first_seen, last_seen, channel, page, username, client_label
         FROM reports
         WHERE last_seen >= :from AND last_seen <= :to
         ORDER BY last_seen DESC'
    );
    $st->bindValue(':from', $from, SQLITE3_INTEGER);
    $st->bindValue(':to', $to, SQLITE3_INTEGER);
    $res = $st->execute();
    $out = [];
    while ($row = $res->fetchArray(SQLITE3_ASSOC)) {
        $sid = (string)$row['session_id'];
        $nSnap = $db->prepare('SELECT COUNT(*) AS n FROM snapshots WHERE session_id = :sid');
        $nSnap->bindValue(':sid', $sid, SQLITE3_TEXT);
        $row['snapshot_count'] = (int)($nSnap->execute()->fetchArray(SQLITE3_ASSOC)['n'] ?? 0);
        $nEvt = $db->prepare('SELECT COUNT(*) AS n FROM events WHERE session_id = :sid');
        $nEvt->bindValue(':sid', $sid, SQLITE3_TEXT);
        $row['event_count'] = (int)($nEvt->execute()->fetchArray(SQLITE3_ASSOC)['n'] ?? 0);
        $last = $db->prepare(
            'SELECT inbound_bps, loss_pct, rtt_ms, rendition, channel
             FROM snapshots WHERE session_id = :sid ORDER BY ts DESC LIMIT 1'
        );
        $last->bindValue(':sid', $sid, SQLITE3_TEXT);
        $ls = $last->execute()->fetchArray(SQLITE3_ASSOC);
        $row['last_inbound_bps'] = $ls['inbound_bps'] ?? null;
        $row['last_loss_pct'] = $ls['loss_pct'] ?? null;
        $row['last_rtt_ms'] = $ls['rtt_ms'] ?? null;
        $row['last_rendition'] = $ls['rendition'] ?? null;
        $out[] = $row;
    }
    return $out;
}

function ce_get(SQLite3 $db, string $sid): array {
    $st = $db->prepare('SELECT * FROM reports WHERE session_id = :sid LIMIT 1');
    $st->bindValue(':sid', $sid, SQLITE3_TEXT);
    $report = $st->execute()->fetchArray(SQLITE3_ASSOC);
    if (!$report) {
        ce_fail(404, 'no client report for that session');
    }
    $snaps = [];
    $q = $db->prepare('SELECT * FROM snapshots WHERE session_id = :sid ORDER BY ts ASC');
    $q->bindValue(':sid', $sid, SQLITE3_TEXT);
    $r = $q->execute();
    while ($row = $r->fetchArray(SQLITE3_ASSOC)) {
        unset($row['id']);
        $snaps[] = $row;
    }
    $evts = [];
    $q2 = $db->prepare('SELECT ts, kind, detail FROM events WHERE session_id = :sid ORDER BY ts ASC');
    $q2->bindValue(':sid', $sid, SQLITE3_TEXT);
    $r2 = $q2->execute();
    while ($row = $r2->fetchArray(SQLITE3_ASSOC)) {
        $evts[] = $row;
    }
    return ['report' => $report, 'snapshots' => $snaps, 'events' => $evts];
}

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

try {
    if ($method === 'POST') {
        if (!auth_bypass_enabled()) {
            auth_migrate();
        }
        $me = auth_require_any();
        auth_session_release();
        $result = ce_ingest(ce_db(), $me, ce_read_json_body());
        ce_ok($result);
    }

    if ($method !== 'GET') {
        ce_fail(405, 'method not allowed');
    }
    if (!auth_bypass_enabled()) {
        auth_migrate();
        auth_require_roles(['admin', 'operator']);
        auth_session_release();
    }
    $action = (string)($_GET['action'] ?? 'list');
    $db = ce_db();
    if ($action === 'list') {
        [$from, $to] = ce_window();
        ce_ok(['from' => $from, 'to' => $to, 'reports' => ce_list($db, $from, $to)]);
    }
    if ($action === 'get') {
        $sid = isset($_GET['session_id']) ? trim((string)$_GET['session_id']) : '';
        if (!ce_session_id_ok($sid)) {
            ce_fail(400, 'session_id must be a WHEP UUID or client report key');
        }
        ce_ok(ce_get($db, $sid));
    }
    ce_fail(400, 'action must be list or get');
} catch (RuntimeException $e) {
    auth_session_release();
    $msg = $e->getMessage();
    if ($msg === 'unauthorized' || $msg === 'forbidden') {
        ce_fail($msg === 'unauthorized' ? 401 : 403, $msg);
    }
    ce_fail(500, $msg);
} catch (Throwable $e) {
    auth_session_release();
    ce_fail(500, 'client-events store unavailable');
}
