<?php
/**
 * NexVUE public front door — DocumentRoot should point here.
 * All UI routes and /api/* are handled by nexvue-web-router.php.
 */
declare(strict_types=1);

require dirname(__DIR__) . '/nexvue-web-router.php';
nexvue_web_dispatch();
