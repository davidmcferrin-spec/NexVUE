<?php
/**
 * NexVUE cloud portal public front door — DocumentRoot should point here.
 * All UI routes and /api/portal are handled by nexvue-portal-web-router.php.
 */
declare(strict_types=1);

require dirname(__DIR__) . '/nexvue-portal-web-router.php';
nexvue_portal_web_dispatch();
