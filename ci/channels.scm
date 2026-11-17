;; SPDX-License-Identifier: MIT
;; Keep CI independent of changes to the install action's bootstrap Guix.
(list
 (channel
  (name 'guix)
  (url "https://git.guix.gnu.org/guix.git")
  (commit "2bc642182e5e7914c1ea4deb696b8e3566b12b3f")
  (introduction
   (make-channel-introduction
    "9edb3f66fd807b096b48283debdcddccfea34bad"
    (openpgp-fingerprint
     "BBB0 2DDF 2CEA F6A8 0D1D  E643 A2A0 6DF2 A33A 54FA")))))
