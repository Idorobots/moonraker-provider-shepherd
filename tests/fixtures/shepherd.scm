;; SPDX-License-Identifier: MIT
;; Only used by the private, unprivileged integration-test Shepherd.
(use-modules (shepherd service))

(register-services
 (list (service '(klipper)
                #:start (make-forkexec-constructor (list (getenv "TEST_SLEEP") "300"))
                #:stop (make-kill-destructor)
                #:respawn? #f)
       (service '(moonraker)
                #:start (make-forkexec-constructor
                         (if (getenv "TEST_MOONRAKER_PYTHON")
                             (list (getenv "TEST_MOONRAKER_PYTHON") "-m" "moonraker"
                                   "-d" (getenv "TEST_MOONRAKER_DATA")
                                   "-c" (getenv "TEST_MOONRAKER_CONFIG"))
                             (list (getenv "TEST_SLEEP") "300")))
                #:stop (make-kill-destructor)
                #:respawn? #f)
       (service '(unmanaged)
                #:start (make-forkexec-constructor (list (getenv "TEST_SLEEP") "300"))
                #:stop (make-kill-destructor)
                #:respawn? #f)))
(for-each (lambda (name) (start-service (lookup-service name)))
          '(klipper moonraker unmanaged))
