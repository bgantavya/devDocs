'use strict';

/**
 * postinstall: fails the install when no Python 3.11+ is found, so the problem
 * shows when installing rather than on first use. DEVDOCS_SKIP_PYTHON_CHECK=1
 * skips the check, for example in an image that adds Python afterwards.
 */

const { findPython, notFoundMessage } = require('../lib/python');

if (process.env.DEVDOCS_SKIP_PYTHON_CHECK !== '1' && !findPython()) {
  console.error(notFoundMessage());
  process.exitCode = 1;
}
