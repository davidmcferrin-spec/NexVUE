#!/usr/bin/env php
<?php
/**
 * nexvue-portal-bootstrap.php — create portal.db, keypair, seed default
 * org + admin user.
 *
 * Run by setup.sh --portal (as root, then smoke-tested as www-data). Safe
 * to re-run: does not reset existing orgs/users or rotate keys.
 *
 * Usage:
 *   php nexvue-portal-bootstrap.php
 * Env overrides: NEXVUE_PORTAL_DB, NEXVUE_PORTAL_DIR, NEXVUE_PORTAL_SEED_ORG_NAME
 */

declare(strict_types=1);

$lib = __DIR__ . '/nexvue-portal-auth-lib.php';
if (!is_file($lib)) {
    $lib = '/usr/local/share/nexvue-portal/nexvue-portal-auth-lib.php';
}
if (!is_file($lib)) {
    fwrite(STDERR, "nexvue-portal-auth-lib.php not found\n");
    exit(1);
}
require_once $lib;

try {
    portal_migrate();
    $keys = portal_ensure_keys();
    $n = (int)portal_db()->querySingle('SELECT COUNT(*) FROM portal_users');
    $orgs = (int)portal_db()->querySingle('SELECT COUNT(*) FROM orgs');
    echo json_encode([
        'ok' => true,
        'db' => portal_db_path(),
        'kid' => $keys['kid'],
        'orgs' => $orgs,
        'users' => $n,
    ]) . "\n";
} catch (Throwable $e) {
    fwrite(STDERR, 'bootstrap failed: ' . $e->getMessage() . "\n");
    exit(1);
}
