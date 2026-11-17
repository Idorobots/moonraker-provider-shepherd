;; SPDX-License-Identifier: MIT
;; Read as data, not evaluated. Install at an immutable absolute path.
((version . 1)
 (socket . "/var/run/shepherd/socket")
 (moonraker-service . "moonraker")
 (services . (("klipper" start stop restart)
              ("moonraker" restart)))
 (power . (poweroff reboot)))
