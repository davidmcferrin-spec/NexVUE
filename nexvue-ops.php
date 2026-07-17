<?php
/**
 * nexvue-ops.php — JSON API for NexVUE Services + Channels ops UI.
 *
 * Phase 1 LAN-trust: no auth. Do not expose on a DMZ without Phase 2 auth.
 * Privileged work goes through allowlisted sudo wrappers only
 * (see nexvue-ops.sudoers / /usr/local/bin/nexvue-ops-*.sh).
 *
 * Actions (GET or POST JSON body):
 *   services | journal | channels_list | channel_get | channel_put
 *   | channels_bulk | restart | aliases
 */

declare(strict_types=1);

header('Content-Type: application/json');
header('Cache-Control: no-store');

const CHANNELS_DIR = '/etc/nexvue/channels';
const SUDO = '/usr/bin/sudo';

const EDITABLE_KEYS = [
    'CHANNEL_ALIAS', 'MAX_DEVICES', 'DEINT_FIELDS', 'BITRATE_KBPS', 'GOP_FRAMES',
    'ENABLE_AUDIO', 'AUDIO_FRAME_MS', 'AUDIO_BITRATE_BPS', 'AUDIO_CHANNELS',
    'DECKLINK_BUFFER_FRAMES', 'VIDEO_ENCODER', 'EXTRA_ENC_ARGS',
    'LO_ENABLE', 'LO_PRESET', 'LO_WIDTH', 'LO_HEIGHT', 'LO_BITRATE_KBPS', 'LO_FPS',
];

function fail(int $status, string $message): never {
    http_response_code($status);
    echo json_encode(['ok' => false, 'error' => $message]);
    exit;
}

function read_json_body(): array {
    $raw = file_get_contents('php://input');
    if ($raw === false || $raw === '') {
        return [];
    }
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        fail(400, 'request body must be JSON object');
    }
    return $data;
}

function sudo_run(array $argv, ?string $stdin = null): array {
    // $argv[0] is the wrapper basename under /usr/local/bin/
    $cmd = [SUDO, '-n'];
    foreach ($argv as $a) {
        $cmd[] = $a;
    }
    $descriptors = [
        0 => ['pipe', 'r'],
        1 => ['pipe', 'w'],
        2 => ['pipe', 'w'],
    ];
    $proc = proc_open($cmd, $descriptors, $pipes, null, null, ['bypass_shell' => true]);
    if (!is_resource($proc)) {
        fail(500, 'failed to start privileged helper');
    }
    if ($stdin !== null) {
        fwrite($pipes[0], $stdin);
    }
    fclose($pipes[0]);
    $stdout = stream_get_contents($pipes[1]);
    $stderr = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $code = proc_close($proc);
    return ['code' => $code, 'stdout' => (string)$stdout, 'stderr' => (string)$stderr];
}

function unit_allowed(string $unit): bool {
    return (bool)preg_match('/^(mediamtx|nexvue-status|nexvue-metrics|nexvue-encode@[0-7])$/', $unit);
}

function list_channel_ids(): array {
    $ids = [];
    for ($i = 0; $i <= 7; $i++) {
        if (is_readable(CHANNELS_DIR . "/{$i}.env")) {
            $ids[] = $i;
        }
    }
    return $ids;
}

function action_from_request(array $body): string {
    $action = $_GET['action'] ?? ($body['action'] ?? '');
    if (!is_string($action) || $action === '') {
        fail(400, 'action required');
    }
    return $action;
}

$body = read_json_body();
$action = action_from_request($body);

// ---- services -----------------------------------------------------------------

if ($action === 'services') {
    $core = ['mediamtx', 'nexvue-status', 'nexvue-metrics'];
    $units = $core;
    foreach (list_channel_ids() as $id) {
        $units[] = "nexvue-encode@{$id}";
    }
    // Always show encode@0-7 slots that have env files; if none, still show core.
    $items = [];
    foreach ($units as $unit) {
        $r = sudo_run(['/usr/local/bin/nexvue-ops-status.sh', $unit]);
        $state = trim($r['stdout']);
        if ($state === '') {
            $state = 'unknown';
        }
        $items[] = [
            'unit' => $unit,
            'state' => $state,
            'ok' => ($state === 'active'),
        ];
    }
    echo json_encode(['ok' => true, 'services' => $items]);
    exit;
}

// ---- journal ------------------------------------------------------------------

if ($action === 'journal') {
    $unit = $body['unit'] ?? ($_GET['unit'] ?? '');
    $lines = (int)($body['lines'] ?? ($_GET['lines'] ?? 100));
    $since = $body['since'] ?? ($_GET['since'] ?? '');
    if (!is_string($unit) || !unit_allowed($unit)) {
        fail(400, 'invalid unit');
    }
    if ($lines < 1) {
        $lines = 1;
    }
    if ($lines > 500) {
        $lines = 500;
    }
    $argv = ['/usr/local/bin/nexvue-ops-journal.sh', $unit, (string)$lines];
    if (is_string($since) && $since !== '') {
        $argv[] = $since;
    }
    $r = sudo_run($argv);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) !== '' ? trim($r['stderr']) : 'journalctl failed');
    }
    echo json_encode(['ok' => true, 'unit' => $unit, 'log' => $r['stdout']]);
    exit;
}

// ---- aliases (lightweight for player/multiview) -------------------------------

if ($action === 'aliases') {
    $aliases = [];
    $devices = [];
    foreach (list_channel_ids() as $id) {
        $r = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
        if ($r['code'] !== 0) {
            continue;
        }
        $data = json_decode($r['stdout'], true);
        if (!is_array($data) || empty($data['ok'])) {
            continue;
        }
        $keys = $data['keys'] ?? [];
        $path = $keys['CHANNEL_PATH'] ?? ("ch{$id}");
        $alias = $keys['CHANNEL_ALIAS'] ?? '';
        $label = $alias !== '' ? $alias : $path;
        $aliases[$path] = $label;
        $aliases[(string)$id] = $label;
        $dev = $keys['DEVICE_NUMBER'] ?? (string)$id;
        $devices[$path] = is_numeric($dev) ? (int)$dev : $id;
    }
    echo json_encode(['ok' => true, 'aliases' => $aliases, 'devices' => $devices]);
    exit;
}

// ---- channels_list ------------------------------------------------------------

if ($action === 'channels_list') {
    $channels = [];
    foreach (list_channel_ids() as $id) {
        $r = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
        if ($r['code'] !== 0) {
            $channels[] = ['id' => $id, 'error' => trim($r['stderr']) ?: 'read failed'];
            continue;
        }
        $data = json_decode($r['stdout'], true);
        if (!is_array($data) || empty($data['ok'])) {
            $channels[] = ['id' => $id, 'error' => 'bad helper output'];
            continue;
        }
        $keys = $data['keys'] ?? [];
        $unit = "nexvue-encode@{$id}";
        $st = sudo_run(['/usr/local/bin/nexvue-ops-status.sh', $unit]);
        $state = trim($st['stdout']) ?: 'unknown';
        $channels[] = [
            'id' => $id,
            'CHANNEL_PATH' => $keys['CHANNEL_PATH'] ?? "ch{$id}",
            'CHANNEL_ALIAS' => $keys['CHANNEL_ALIAS'] ?? '',
            'DEVICE_NUMBER' => $keys['DEVICE_NUMBER'] ?? (string)$id,
            'DEINT_FIELDS' => $keys['DEINT_FIELDS'] ?? '',
            'BITRATE_KBPS' => $keys['BITRATE_KBPS'] ?? '',
            'ENABLE_AUDIO' => $keys['ENABLE_AUDIO'] ?? '',
            'LO_ENABLE' => $keys['LO_ENABLE'] ?? '',
            'LO_PRESET' => $keys['LO_PRESET'] ?? '',
            'unit' => $unit,
            'state' => $state,
            'active' => ($state === 'active'),
        ];
    }
    echo json_encode(['ok' => true, 'channels' => $channels, 'editable_keys' => EDITABLE_KEYS]);
    exit;
}

// ---- channel_get --------------------------------------------------------------

if ($action === 'channel_get') {
    $id = $body['id'] ?? ($_GET['id'] ?? null);
    if (!is_numeric($id) || (int)$id < 0 || (int)$id > 7) {
        fail(400, 'id must be 0-7');
    }
    $id = (int)$id;
    $r = sudo_run(['/usr/local/bin/nexvue-ops-env-read.sh', (string)$id]);
    if ($r['code'] !== 0) {
        fail(404, trim($r['stderr']) ?: 'channel not found');
    }
    $data = json_decode($r['stdout'], true);
    if (!is_array($data) || empty($data['ok'])) {
        fail(500, 'bad helper output');
    }
    echo json_encode([
        'ok' => true,
        'id' => $id,
        'keys' => $data['keys'] ?? [],
        'editable_keys' => EDITABLE_KEYS,
        'readonly_keys' => ['DEVICE_NUMBER', 'CHANNEL_PATH', 'RTSP_URL'],
    ]);
    exit;
}

// ---- channel_put --------------------------------------------------------------

if ($action === 'channel_put') {
    $id = $body['id'] ?? null;
    $patch = $body['patch'] ?? null;
    if (!is_numeric($id) || (int)$id < 0 || (int)$id > 7) {
        fail(400, 'id must be 0-7');
    }
    if (!is_array($patch)) {
        fail(400, 'patch object required');
    }
    $id = (int)$id;
    $clean = [];
    foreach ($patch as $k => $v) {
        if (!is_string($k) || !in_array($k, EDITABLE_KEYS, true)) {
            fail(400, "key not editable: {$k}");
        }
        if (!is_scalar($v)) {
            fail(400, "bad value for {$k}");
        }
        $clean[$k] = (string)$v;
    }
    $r = sudo_run(
        ['/usr/local/bin/nexvue-ops-env-write.sh', (string)$id],
        json_encode($clean, JSON_UNESCAPED_SLASHES)
    );
    if ($r['code'] !== 0) {
        $err = trim($r['stderr']);
        $parsed = json_decode($err, true);
        fail(400, is_array($parsed) ? ($parsed['error'] ?? $err) : ($err ?: 'write failed'));
    }
    $data = json_decode($r['stdout'], true);
    echo json_encode([
        'ok' => true,
        'id' => $id,
        'keys' => is_array($data) ? ($data['keys'] ?? []) : [],
        'restart_units' => ["nexvue-encode@{$id}"],
    ]);
    exit;
}

// ---- channels_bulk ------------------------------------------------------------

if ($action === 'channels_bulk') {
    $ids = $body['ids'] ?? null;
    $patch = $body['patch'] ?? null;
    if (!is_array($ids) || $ids === []) {
        fail(400, 'ids array required');
    }
    if (!is_array($patch) || $patch === []) {
        fail(400, 'patch object required');
    }
    $clean = [];
    foreach ($patch as $k => $v) {
        if (!is_string($k) || !in_array($k, EDITABLE_KEYS, true)) {
            fail(400, "key not editable: {$k}");
        }
        if (!is_scalar($v)) {
            fail(400, "bad value for {$k}");
        }
        $clean[$k] = (string)$v;
    }
    $updated = [];
    $restart = [];
    foreach ($ids as $rawId) {
        if (!is_numeric($rawId) || (int)$rawId < 0 || (int)$rawId > 7) {
            fail(400, 'each id must be 0-7');
        }
        $id = (int)$rawId;
        $r = sudo_run(
            ['/usr/local/bin/nexvue-ops-env-write.sh', (string)$id],
            json_encode($clean, JSON_UNESCAPED_SLASHES)
        );
        if ($r['code'] !== 0) {
            $err = trim($r['stderr']);
            $parsed = json_decode($err, true);
            fail(400, "channel {$id}: " . (is_array($parsed) ? ($parsed['error'] ?? $err) : ($err ?: 'write failed')));
        }
        $updated[] = $id;
        $restart[] = "nexvue-encode@{$id}";
    }
    echo json_encode(['ok' => true, 'updated' => $updated, 'restart_units' => $restart]);
    exit;
}

// ---- restart ------------------------------------------------------------------

if ($action === 'restart') {
    $units = $body['units'] ?? null;
    if (!is_array($units) || $units === []) {
        fail(400, 'units array required');
    }
    $clean = [];
    foreach ($units as $u) {
        if (!is_string($u) || !unit_allowed($u)) {
            fail(400, "disallowed unit: {$u}");
        }
        $clean[] = $u;
    }
    $argv = array_merge(['/usr/local/bin/nexvue-ops-restart.sh'], $clean);
    $r = sudo_run($argv);
    if ($r['code'] !== 0) {
        fail(500, trim($r['stderr']) ?: 'restart failed');
    }
    echo json_encode(['ok' => true, 'restarted' => $clean]);
    exit;
}

fail(400, 'unknown action');
