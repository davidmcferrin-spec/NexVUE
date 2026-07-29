#!/usr/bin/env php
<?php
/**
 * nexvue-auth-bootstrap.php — create auth.db, keypair, seed admin, publish JWT.
 *
 * Run by setup.sh (as root). Safe to re-run: does not reset existing users or
 * rotate keys/JWT if already present.
 *
 * Usage:
 *   php nexvue-auth-bootstrap.php
 * Env overrides: NEXVUE_AUTH_DB, NEXVUE_AUTH_DIR, NEXVUE_STATION_ENV
 */

declare(strict_types=1);

$lib = __DIR__ . '/nexvue-auth-lib.php';
if (!is_file($lib)) {
    $lib = '/usr/local/share/nexvue/nexvue-auth-lib.php';
}
if (!is_file($lib)) {
    fwrite(STDERR, "nexvue-auth-lib.php not found\n");
    exit(1);
}
require_once $lib;

try {
    auth_migrate();
    $keys = auth_ensure_keys();
    $jwt = auth_ensure_publish_jwt_in_env();
    $n = (int)auth_db()->querySingle('SELECT COUNT(*) FROM users');
    fwrite(STDOUT, "auth bootstrap ok: users={$n} kid={$keys['kid']} publish_jwt_len=" . strlen($jwt) . "\n");
    exit(0);
} catch (Throwable $e) {
    fwrite(STDERR, 'auth bootstrap failed: ' . $e->getMessage() . "\n");
    exit(1);
}
