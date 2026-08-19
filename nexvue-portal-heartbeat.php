#!/usr/bin/env php
<?php
/**
 * nexvue-portal-heartbeat.php — periodic outbound sync to an adopted portal
 * (Phase 4). Run by nexvue-portal-heartbeat.timer as www-data — needs no
 * root/sudo: only reads world-readable env files and writes inside the
 * already www-data-owned auth dir (JWKS cache + heartbeat-status.json).
 *
 * No-ops (exit 0) immediately when this station isn't adopted, so the timer
 * can be unconditionally enabled on every station with zero effect until an
 * admin runs "Adopt this station" (portal_enroll in nexvue-auth.php).
 *
 * Never throws past main(): a portal outage must never fail this unit —
 * that would restart-loop it and spam the journal. Every failure path
 * writes {"ok": false, "at": ...} to the status file and exits 0.
 */

declare(strict_types=1);

$lib = __DIR__ . '/nexvue-auth-lib.php';
if (!is_file($lib)) {
    // Repo checkout: lib lives under web-node/ (this script stays at repo
    // root — it's a CLI installer/timer target, not served web content).
    $lib = __DIR__ . '/web-node/nexvue-auth-lib.php';
}
if (!is_file($lib)) {
    // Deployed box: setup.sh installs the lib here regardless of repo layout.
    $lib = '/usr/local/share/nexvue/nexvue-auth-lib.php';
}
if (!is_file($lib)) {
    fwrite(STDERR, "nexvue-auth-lib.php not found\n");
    exit(0); // still don't fail the unit
}
require_once $lib;

const NEXVUE_PORTAL_HEARTBEAT_MAX_CHANNEL_ID = 7;

function heartbeat_channels_dir(): string {
    $o = getenv('NEXVUE_CHANNELS_DIR');
    return (is_string($o) && $o !== '') ? rtrim($o, '/\\') : '/etc/nexvue/channels';
}

function heartbeat_write_status(bool $ok): void {
    try {
        $dir = dirname(auth_portal_heartbeat_status_path());
        if (!is_dir($dir)) {
            @mkdir($dir, 0750, true);
        }
        file_put_contents(
            auth_portal_heartbeat_status_path(),
            json_encode(['ok' => $ok, 'at' => auth_now_iso()], JSON_UNESCAPED_SLASHES)
        );
    } catch (Throwable $e) {
        // Best-effort — never let status-file trouble fail the unit.
    }
}

/** @return list<array{channel_base:string, alias:string, lo_enabled:bool, active:bool}> */
function heartbeat_collect_channels(): array {
    $out = [];
    for ($i = 0; $i <= NEXVUE_PORTAL_HEARTBEAT_MAX_CHANNEL_ID; $i++) {
        $path = heartbeat_channels_dir() . "/{$i}.env";
        if (!is_readable($path)) {
            continue;
        }
        $raw = @file_get_contents($path);
        if (!is_string($raw)) {
            continue;
        }
        $keys = [];
        foreach (preg_split("/\r\n|\n|\r/", $raw) ?: [] as $line) {
            $line = trim($line);
            if ($line === '' || str_starts_with($line, '#') || !str_contains($line, '=')) {
                continue;
            }
            [$k, $v] = explode('=', $line, 2);
            $k = trim($k);
            if ($k === '' || !preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/', $k)) {
                continue;
            }
            $v = trim($v);
            if ((str_starts_with($v, '"') && str_ends_with($v, '"'))
                || (str_starts_with($v, "'") && str_ends_with($v, "'"))) {
                $v = substr($v, 1, -1);
            } elseif (!str_contains($v, '"') && !str_contains($v, "'") && str_contains($v, '#')) {
                $v = trim(explode('#', $v, 2)[0]);
            }
            $keys[$k] = $v;
        }
        $out[] = [
            'channel_base' => $keys['CHANNEL_PATH'] ?? "ch{$i}",
            'alias' => $keys['CHANNEL_ALIAS'] ?? '',
            'lo_enabled' => strtolower((string)($keys['LO_ENABLE'] ?? '')) === 'true',
            'active' => true,
        ];
    }
    return $out;
}

function main(): int {
    if (!auth_portal_adopted()) {
        exit(0);
    }
    $env = auth_read_portal_env();
    if ($env['url'] === '' || $env['station_api_key'] === '') {
        heartbeat_write_status(false);
        return 0;
    }
    $edgeVersion = '';
    $vp = __DIR__ . '/VERSION';
    if (is_readable($vp)) {
        $edgeVersion = trim((string)file_get_contents($vp));
    }
    $r = auth_portal_http_post(
        $env['url'] . '/api/portal?action=station_heartbeat',
        [
            'status' => 'active',
            'edge_version' => $edgeVersion,
            'channels' => heartbeat_collect_channels(),
        ],
        $env['station_api_key']
    );
    if ($r['status'] < 200 || $r['status'] >= 300) {
        heartbeat_write_status(false);
        return 0;
    }
    $data = json_decode($r['body'], true);
    if (!is_array($data) || empty($data['ok'])) {
        heartbeat_write_status(false);
        return 0;
    }
    if (isset($data['portal_jwks']) && is_array($data['portal_jwks'])) {
        try {
            auth_portal_jwks_cache_write($data['portal_jwks']);
        } catch (Throwable $e) {
            // Non-fatal — heartbeat itself succeeded; try again next cycle.
        }
    }
    heartbeat_write_status(true);
    return 0;
}

// Test harness includes this file to exercise individual functions without
// triggering a real run (which would call exit() before assertions).
if (getenv('NEXVUE_PORTAL_HEARTBEAT_INCLUDE_ONLY') !== '1') {
    exit(main());
}
