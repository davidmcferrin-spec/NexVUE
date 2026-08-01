<?php
/**
 * nexvue-web-router.php — path front door for the NexVUE edge UI + /api/*.
 *
 * App root (parent of public/): pages/, nexvue-*.php, VERSION.
 * DocumentRoot must be {app}/public so those files are not web-enumerable.
 *
 * Routes:
 *   / /player /multiview /metrics /settings /services /users
 *   /login /forgot /reset
 *   /s/{token}[/multiview]  — share redeem then player/multiview
 *   /api/{auth|ops|metrics|status|mediamtx|captions|logo|version|jwks}
 * Legacy *.html and nexvue-*.php paths → 301/internal to the new routes.
 */

declare(strict_types=1);

function nexvue_app_root(): string {
    static $root = null;
    if ($root !== null) {
        return $root;
    }
    $env = getenv('NEXVUE_APP_ROOT');
    if (is_string($env) && $env !== '') {
        $root = rtrim($env, '/\\');
        return $root;
    }
    // public/index.php → parent; or this file lives in app root.
    $here = __DIR__;
    if (is_dir($here . '/pages') || is_file($here . '/nexvue-auth.php')) {
        $root = $here;
        return $root;
    }
    $parent = dirname($here);
    $root = $parent;
    return $root;
}

function nexvue_web_request_path(): string {
    $uri = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url($uri, PHP_URL_PATH);
    if (!is_string($path) || $path === '') {
        $path = '/';
    }
    // Normalize trailing slash (except root).
    if ($path !== '/' && str_ends_with($path, '/')) {
        $path = rtrim($path, '/');
    }
    return $path;
}

/** @return never */
function nexvue_web_redirect(string $to, int $code = 302): void {
    if (!str_starts_with($to, 'http') && !str_starts_with($to, '/')) {
        $to = '/' . $to;
    }
    header('Location: ' . $to, true, $code);
    exit;
}

function nexvue_web_load_auth(): void {
    require_once nexvue_app_root() . '/nexvue-auth-lib.php';
}

/**
 * Page table: path => [file under pages/, roles|null, allowShare, public].
 *
 * @return array<string, array{file:string, roles:?list<string>, share:bool, public:bool}>
 */
function nexvue_web_pages(): array {
    return [
        '/' => ['file' => 'index.html', 'roles' => null, 'share' => true, 'public' => false],
        '/player' => ['file' => 'index.html', 'roles' => null, 'share' => true, 'public' => false],
        '/multiview' => ['file' => 'multiview.html', 'roles' => null, 'share' => true, 'public' => false],
        '/metrics' => ['file' => 'metrics.html', 'roles' => ['admin', 'operator'], 'share' => false, 'public' => false],
        '/settings' => ['file' => 'channels.html', 'roles' => ['admin', 'operator'], 'share' => false, 'public' => false],
        '/services' => ['file' => 'services.html', 'roles' => ['admin'], 'share' => false, 'public' => false],
        '/users' => ['file' => 'users.html', 'roles' => ['admin'], 'share' => false, 'public' => false],
        '/login' => ['file' => 'login.html', 'roles' => null, 'share' => false, 'public' => true],
        '/forgot' => ['file' => 'forgot.html', 'roles' => null, 'share' => false, 'public' => true],
        '/reset' => ['file' => 'reset.html', 'roles' => null, 'share' => false, 'public' => true],
    ];
}

/** @return array<string, string> path => php basename in app root */
function nexvue_web_apis(): array {
    return [
        '/api/auth' => 'nexvue-auth.php',
        '/api/ops' => 'nexvue-ops.php',
        '/api/metrics' => 'nexvue-metrics.php',
        '/api/status' => 'nexvue-status.php',
        '/api/mediamtx' => 'nexvue-mediamtx-api.php',
        '/api/captions' => 'nexvue-captions.php',
        '/api/logo' => 'nexvue-logo.php',
        '/api/version' => 'nexvue-version.php',
        '/api/jwks' => 'nexvue-jwks.php',
    ];
}

/** Legacy path → new path (301). Query string preserved by caller. */
function nexvue_web_legacy_page_redirect(string $path): ?string {
    $map = [
        '/index.html' => '/player',
        '/multiview.html' => '/multiview',
        '/metrics.html' => '/metrics',
        '/channels.html' => '/settings',
        '/services.html' => '/services',
        '/users.html' => '/users',
        '/login.html' => '/login',
        '/forgot.html' => '/forgot',
        '/reset.html' => '/reset',
    ];
    return $map[$path] ?? null;
}

function nexvue_web_legacy_api_path(string $path): ?string {
    $map = [
        '/nexvue-auth.php' => '/api/auth',
        '/nexvue-ops.php' => '/api/ops',
        '/nexvue-metrics.php' => '/api/metrics',
        '/nexvue-status.php' => '/api/status',
        '/nexvue-mediamtx-api.php' => '/api/mediamtx',
        '/nexvue-captions.php' => '/api/captions',
        '/nexvue-logo.php' => '/api/logo',
        '/nexvue-version.php' => '/api/version',
        '/nexvue-jwks.php' => '/api/jwks',
    ];
    return $map[$path] ?? null;
}

function nexvue_web_query_suffix(): string {
    $q = $_SERVER['QUERY_STRING'] ?? '';
    return ($q !== '') ? ('?' . $q) : '';
}

/**
 * Redeem ?t= or path token into a session when present.
 */
function nexvue_web_try_share_token(?string $token): void {
    if ($token === null || $token === '') {
        return;
    }
    nexvue_web_load_auth();
    try {
        auth_migrate();
    } catch (Throwable $e) {
        return;
    }
    $row = auth_share_find_by_token($token);
    if (!auth_share_is_valid($row)) {
        return;
    }
    auth_login_share($row);
}

/**
 * @param array{file:string, roles:?list<string>, share:bool, public:bool} $page
 */
function nexvue_web_authorize_page(array $page, string $path): void {
    if ($page['public']) {
        return;
    }
    nexvue_web_load_auth();
    try {
        auth_migrate();
    } catch (Throwable $e) {
        nexvue_web_redirect('/login?next=' . rawurlencode($path . nexvue_web_query_suffix()));
    }

    // Prefer ?t= on the URL for first paint of share links.
    $t = $_GET['t'] ?? null;
    if (is_string($t) && $t !== '') {
        nexvue_web_try_share_token($t);
    }

    $me = auth_me_payload();
    if ($me === null) {
        nexvue_web_redirect('/login?next=' . rawurlencode($path . nexvue_web_query_suffix()));
    }
    if (!empty($me['must_change_password']) && ($me['auth'] ?? '') === 'user') {
        nexvue_web_redirect('/login?change=1&next=' . rawurlencode($path . nexvue_web_query_suffix()));
    }
    if (($me['auth'] ?? '') === 'share') {
        if (!$page['share']) {
            nexvue_web_redirect('/login?next=' . rawurlencode($path));
        }
        return;
    }
    $roles = $page['roles'];
    if (is_array($roles) && $roles !== []) {
        $role = (string)($me['role'] ?? '');
        if (!in_array($role, $roles, true)) {
            nexvue_web_redirect('/player');
        }
    }
}

function nexvue_web_serve_page(string $file): void {
    $path = nexvue_app_root() . '/pages/' . $file;
    if (!is_file($path) || !is_readable($path)) {
        http_response_code(500);
        header('Content-Type: text/plain; charset=utf-8');
        echo "NexVUE page missing: {$file}\n";
        exit;
    }
    header('Content-Type: text/html; charset=utf-8');
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    readfile($path);
    exit;
}

function nexvue_web_dispatch_api(string $phpFile): void {
    $full = nexvue_app_root() . '/' . $phpFile;
    if (!is_file($full)) {
        http_response_code(500);
        header('Content-Type: application/json');
        echo json_encode(['ok' => false, 'error' => 'api handler missing']);
        exit;
    }
    // Handlers are written as HTTP entrypoints (exit on completion).
    require $full;
    exit;
}

function nexvue_web_dispatch(): void {
    $path = nexvue_web_request_path();

    // Static assets under public/assets/ are served by Apache directly.
    // If we got here for /assets/*, file was missing.
    if (str_starts_with($path, '/assets/')) {
        http_response_code(404);
        header('Content-Type: text/plain; charset=utf-8');
        echo "not found\n";
        exit;
    }

    // Legacy HTML bookmarks.
    $legacyPage = nexvue_web_legacy_page_redirect($path);
    if ($legacyPage !== null) {
        nexvue_web_redirect($legacyPage . nexvue_web_query_suffix(), 301);
    }

    // Legacy API filenames → path API (307 keeps method/body).
    $legacyApi = nexvue_web_legacy_api_path($path);
    if ($legacyApi !== null) {
        nexvue_web_redirect($legacyApi . nexvue_web_query_suffix(), 307);
    }

    // /s/<token> or /s/<token>/multiview
    if (preg_match('#^/s/([A-Za-z0-9]+)(?:/(multiview|player))?$#', $path, $m)) {
        $token = $m[1];
        $dest = (isset($m[2]) && $m[2] === 'multiview') ? '/multiview' : '/player';
        nexvue_web_try_share_token($token);
        nexvue_web_redirect($dest . '?t=' . rawurlencode($token), 302);
    }

    $apis = nexvue_web_apis();
    if (isset($apis[$path])) {
        nexvue_web_dispatch_api($apis[$path]);
    }

    $pages = nexvue_web_pages();
    if (isset($pages[$path])) {
        $page = $pages[$path];
        nexvue_web_authorize_page($page, $path);
        nexvue_web_serve_page($page['file']);
    }

    http_response_code(404);
    header('Content-Type: text/plain; charset=utf-8');
    echo "not found\n";
    exit;
}
