(function () {
    'use strict';

    function closeDialogs() {
        var dialogs = document.querySelectorAll('.sweet-alert, .sweet-overlay');
        for (var i = 0; i < dialogs.length; i++) {
            dialogs[i].style.display = 'none';
            dialogs[i].setAttribute('aria-hidden', 'true');
        }
    }

    function callIfPresent(callback, value) {
        if (typeof callback === 'function') {
            callback(value);
        }
    }

    function patchTaskCounts(injector, common) {
        if (common.__scannerTaskCounts) {
            return;
        }

        var settings = injector.get('aria2SettingService');
        var countTargets = {
            downloading: 'numActive',
            waiting: 'numWaiting',
            stopped: 'numStopped'
        };

        function updateCounts(stat) {
            if (!stat) {
                return;
            }

            var values = stat.data || stat;

            Object.keys(countTargets).forEach(function (name) {
                var link = document.querySelector('a[href="#!/' + name + '"]');
                if (!link) {
                    return;
                }

                if (name === 'stopped') {
                    var nativeLabel = link.querySelector('span:not(.scanner-task-count)');
                    if (nativeLabel && !nativeLabel.classList.contains('scanner-task-label')) {
                        // Detach Angular's native binding so Scanner can style the count separately.
                        var label = nativeLabel.cloneNode(true);
                        label.classList.add('scanner-task-label');
                        label.removeAttribute('ng-bind');
                        label.textContent = nativeLabel.textContent.replace(/\s*\(\d+\)\s*$/, '');
                        nativeLabel.replaceWith(label);
                    }
                }

                var count = link.querySelector('.scanner-task-count');
                if (!count) {
                    count = document.createElement('span');
                    count.className = 'scanner-task-count';
                    link.appendChild(count);
                }

                var value = String(Number(values[countTargets[name]] || 0));
                count.textContent = '(' + value + ')';
            });
        }

        function refreshCounts() {
            settings.getGlobalStat(updateCounts, true);
        }

        common.__scannerTaskCounts = true;
        refreshCounts();
        window.setInterval(refreshCounts, 3000);
    }

    function patchDialogs() {
        if (!window.angular) {
            return false;
        }

        var app = document.querySelector('[ng-app]');
        var injector = app && window.angular.element(app).injector();
        if (!injector) {
            return false;
        }

        var common = injector.get('ariaNgCommonService');
        if (!common || common.__scannerQuietDialogs) {
            return true;
        }

        common.__scannerQuietDialogs = true;
        common.showDialog = function (title, text, type, callback) {
            closeDialogs();
            callIfPresent(callback);
        };
        common.showInfo = function (title, text, callback) {
            closeDialogs();
            callIfPresent(callback);
        };
        common.showError = function (text, callback) {
            closeDialogs();
            callIfPresent(callback);
        };
        common.showOperationSucceeded = function (text, callback) {
            closeDialogs();
            callIfPresent(callback);
        };
        common.confirm = function (title, text, type, callback) {
            closeDialogs();
            callIfPresent(callback, true);
        };
        common.closeAllDialogs = closeDialogs;
        patchTaskCounts(injector, common);

        try {
            var sweetAlert = injector.get('SweetAlert');
            sweetAlert.swal = function (options, callback) {
                closeDialogs();
                callIfPresent(callback, true);
            };
        } catch (_) {
            // ariaNgCommonService remains sufficient when the optional service is unavailable.
        }

        closeDialogs();
        return true;
    }

    var timer = window.setInterval(function () {
        closeDialogs();
        if (patchDialogs()) {
            window.clearInterval(timer);
        }
    }, 25);
}());
