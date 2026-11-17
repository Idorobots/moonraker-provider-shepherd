;; SPDX-License-Identifier: MIT
(use-modules (moonraker shepherd) (srfi srfi-64) (ice-9 match))

(test-begin "shepherd-helper")
(define runner (test-runner-current))

(define policy
  '((version . 1) (socket . "/private/socket") (moonraker-service . "moonraker")
    (services . (("klipper" start stop restart) ("moonraker" restart)))
    (power . (poweroff reboot halt))))

(define* (service name #:key (requires '()) (status 'running) (pid 123)
                  (failures '()) (changes '()) (enabled? #t) (exits '()))
  `(service (version 0) (provides (,name)) (requires ,requires)
            (running ,(if (eq? status 'stopped) #f `(process (version 0) (id ,pid) (command ("sleep")))))
            (status ,status) (enabled? ,enabled?) (startup-failures ,failures)
            (status-changes ,changes) (exit-statuses ,exits) (docstring "Fixture")))

(define (error-code thunk)
  (catch 'management-error (lambda () (thunk) #f) (lambda (_ code message) code)))

(test-equal "valid policy" policy (validate-policy policy))
(for-each
 (lambda (replacement)
   (test-equal "invalid policy rejected" "policy"
     (error-code (lambda () (validate-policy (cons replacement (assq-remove! (list-copy policy) (car replacement))))))))
 '((version . 2) (socket . "relative") (moonraker-service . "root")
   (services . (("root" restart))) (services . (("klipper;reboot" stop)))
   (services . (("moonraker" start stop restart)))
   (services . (("moonraker" restart) ("moonraker" restart)))
   (services . (("moonraker" eval))) (power . (reconfigure))))
(test-equal "duplicate header rejected" "policy"
  (error-code (lambda () (validate-policy (cons '(version . 1) policy)))))

(for-each
 (match-lambda
   ((status active sub)
    (let ((state (service-state (service 'klipper #:status status))))
      (test-equal "active state" active (assoc-ref state "active_state"))
      (test-equal "sub state" sub (assoc-ref state "sub_state")))))
 '((running "active" "running") (stopped "inactive" "dead")
   (starting "activating" "starting") (stopping "deactivating" "stopping")
   (unfamiliar "unknown" "unknown")))
(test-equal "failed constructor" "failed"
  (assoc-ref (service-state (service 'klipper #:status 'stopped #:failures '(20))) "active_state"))
(test-equal "recovered constructor and later deliberate stop" "inactive"
  (assoc-ref (service-state (service 'klipper #:status 'stopped #:failures '(20)
                                    #:changes '((stopped . 40) (running . 30)))) "active_state"))
(test-equal "disabled after failing exits" "failed"
  (assoc-ref (service-state (service 'klipper #:status 'stopped #:enabled? #f #:exits '((256 . 40)))) "active_state"))
(test-equal "PID extraction" 123 (assoc-ref (service-details (service 'klipper) '(restart)) "pid"))
(test-equal "stopped PID is null" 'null
  (assoc-ref (service-details (service 'klipper #:status 'stopped) '(restart)) "pid"))

(define services (list (service 'klipper) (service 'moonraker)))
(define commands '())
(define queued '())
(define (request policy action target)
  (if (eq? action 'status) services
      (begin (set! commands (cons (list action target) commands)) #t)))
(define (queue thunk) (set! queued (cons thunk queued)) '(("accepted" . #t)))
(define (run . args) (dispatch policy args #:request request #:queue queue))

(test-equal "inventory keys" '("klipper" "moonraker")
  (map car (assoc-ref (run "status") "services")))
(test-equal "normal stop completes synchronously" #f (assoc-ref (run "stop" "klipper") "accepted"))
(test-equal "ordinary stop dispatch" '(stop klipper) (car commands))
(for-each
 (lambda (args)
   (test-equal "undeclared action rejected" "denied"
     (error-code (lambda () (apply run args)))))
 '(("eval" "klipper") ("stop" "root") ("stop" "moonraker")
   ("start" "klipper;reboot") ("start" "other") ("restart" "klipper" "extra")))
(test-equal "self restart acknowledged" #t (assoc-ref (run "restart" "moonraker") "accepted"))
(test-equal "self restart has not yet executed" '(stop klipper) (car commands))
((car queued))
(test-equal "queued self restart" '(restart moonraker) (car commands))
(for-each
 (match-lambda
   ((command action)
    (run command)
    ((car queued))
    (test-equal "power dispatch uses root protocol action" (list action 'root) (car commands))))
 '(("poweroff" power-off) ("reboot" stop) ("halt" halt)))

(set! services (list (service 'klipper) (service 'intermediate #:requires '(klipper))
                     (service 'moonraker #:requires '(intermediate))))
(test-equal "transitive Moonraker dependency rejected" "denied"
  (error-code (lambda () (run "stop" "klipper"))))
(set! services (list (service 'root #:requires '())
                     '(service (version 0) (provides (root shepherd klipper)))))
(test-equal "root alias rejected" "denied"
  (error-code (lambda () (run "restart" "klipper"))))
(set! services (list (service 'moonraker)))
(test-equal "missing service rejected" "not-found"
  (error-code (lambda () (run "start" "klipper"))))
(set! services (list (service 'klipper) (service 'moonraker)))
(test-equal "constructor returning false is an error" "shepherd"
  (error-code (lambda () (dispatch policy '("start" "klipper")
                                  #:request (lambda (_ action target)
                                              (if (eq? action 'status) services #f))))))

(test-end "shepherd-helper")
(exit (if (zero? (test-runner-fail-count runner)) 0 1))
