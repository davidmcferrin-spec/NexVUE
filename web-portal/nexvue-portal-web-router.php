<?php
/**
 * nexvue-portal-web-router.php — path front door for the NexVUE cloud
 * portal UI + /api/portal.
 *
 * App root (parent of public/): pages/, nexvue-portal-*.php.
 * DocumentRoot must be {app}/public so those files are not web-enumerable.
 *
 * Routes:
 *   /login /catalog /watch /stations /users
 *   /api/portal
 */

declare(strict_types=1);

function nexvue_portal_app_root(): string {
    static $root = null;
    if ($root !== null) {
        return $root;
    }
    $env = getenv('NEXVUE_PORTAL_APP_ROOT');
    if (is_string($env) && $env !== '') {
        $root = rtrim($env, '/\\');
        return $root;
    }
    $here = __DIR__;
    if (is_dir($here . '/pages') || is_file($here . '/nexvue-portal-api.php')) {
        $root = $here;
        return $root;
    }
    $root = dirname($here);
    return $root;
}

function nexvue_portal_web_request_path(): string {
    $uri = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url($uri, PHP_URL_PATH);
    if (!is_string($path) || $path === '') {
        $path = '/';
    }
    if ($path !== '/' && str_ends_with($path, '/')) {
        $path = rtrim($path, '/');
    }
    return $path;
}

/** @return never */
function nexvue_portal_web_redirect(string $to, int $code = 302): void {
    if (!str_starts_with($to, 'http') && !str_starts_with($to, '/')) {
        $to = '/' . $to;
    }
    header('Location: ' . $to, true, $code);
    exit;
}

function nexvue_portal_web_load_auth(): void {
    require_once nexvue_portal_app_root() . '/nexvue-portal-auth-lib.php';
}

/**
 * Page table: path => [file under pages/, roles|null, public].
 *
 * @return array<string, array{file:string, roles:?list<string>, public:bool}>
 */
function nexvue_portal_web_pages(): array {
    return [
        '/' => ['file' => 'catalog.html', 'roles' => null, 'public' => false],
        '/login' => ['file' => 'login.html', 'roles' => null, 'public' => true],
        '/catalog' => ['file' => 'catalog.html', 'roles' => null, 'public' => false],
        '/watch' => ['file' => 'watch.html', 'roles' => null, 'public' => false],
        '/stations' => ['file' => 'stations.html', 'roles' => ['org_admin'], 'public' => false],
        '/users' => ['file' => 'users.html', 'roles' => ['org_admin'], 'public' => false],
    ];
}

function nexvue_portal_web_query_suffix(): string {
    $q = $_SERVER['QUERY_STRING'] ?? '';
    return ($q !== '') ? ('?' . $q) : '';
}

/**
 * @param array{file:string, roles:?list<string>, public:bool} $page
 */
function nexvue_portal_web_authorize_page(array $page, string $path): void {
    if ($page['public']) {
        return;
    }
    nexvue_portal_web_load_auth();
    try {
        portal_migrate();
    } catch (Throwable $e) {
        nexvue_portal_web_redirect('/login?next=' . rawurlencode($path . nexvue_portal_web_query_suffix()));
    }
    $me = portal_me_payload();
    if ($me === null) {
        nexvue_portal_web_redirect('/login?next=' . rawurlencode($path . nexvue_portal_web_query_suffix()));
    }
    if (!empty($me['must_change_password'])) {
        nexvue_portal_web_redirect('/login?change=1&next=' . rawurlencode($path . nexvue_portal_web_query_suffix()));
    }
    $roles = $page['roles'];
    if (is_array($roles) && $roles !== []) {
        $role = (string)($me['role'] ?? '');
        if (!in_array($role, $roles, true)) {
            nexvue_portal_web_redirect('/catalog');
        }
    }
}

function nexvue_portal_web_serve_page(string $file): void {
    $path = nexvue_portal_app_root() . '/pages/' . $file;
    if (!is_file($path) || !is_readable($path)) {
        http_response_code(500);
        header('Content-Type: text/plain; charset=utf-8');
        echo "NexVUE portal page missing: {$file}\n";
        exit;
    }
    header('Content-Type: text/html; charset=utf-8');
    header('Cache-Control: no-store');
    header('X-Content-Type-Options: nosniff');
    readfile($path);
    exit;
}

function nexvue_portal_web_dispatch_api(): void {
    $full = nexvue_portal_app_root() . '/nexvue-portal-api.php';
    if (!is_file($full)) {
        http_response_code(500);
        header('Content-Type: application/json');
        echo json_encode(['ok' => false, 'error' => 'api handler missing']);
        exit;
    }
    require $full;
    exit;
}

function nexvue_portal_web_dispatch(): void {
    $path = nexvue_portal_web_request_path();

    if (str_starts_with($path, '/assets/')) {
        http_response_code(404);
        header('Content-Type: text/plain; charset=utf-8');
        echo "not found\n";
        exit;
    }

    if ($path === '/api/portal') {
        nexvue_portal_web_dispatch_api();
    }

    $pages = nexvue_portal_web_pages();
    if (isset($pages[$path])) {
        $page = $pages[$path];
        nexvue_portal_web_authorize_page($page, $path);
        nexvue_portal_web_serve_page($page['file']);
    }

    http_response_code(404);
    header('Content-Type: text/plain; charset=utf-8');
    echo "not found\n";
    exit;
}
