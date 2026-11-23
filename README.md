# moonraker-provider-shepherd

A GNU Shepherd machine provider for Moonraker, intended for declaratively
managed Guix System hosts. **MIT licensed.**

The project contains a Python provider, a Guile management helper, and a small
Moonraker integration patch. Moonraker runs as its normal unprivileged account;
the helper performs policy-limited management through Shepherd's control socket.

## Features

- Discover declared services and report running, stopped, failed, and transitional
  states through Moonraker's machine API.
- Publish `machine:service_state_changed` notifications, including connection loss
  and recovery.
- Start, stop, and restart Klipper and other declared services.
- Restart Moonraker through a detached worker that survives Shepherd's
  process-group termination.
- Request orderly reboot, power-off, or halt through Shepherd's root service.
- Identify services by PID for Moonraker's service reporting and existing Klippy
  log-rollover workflow.
- Check the helper's noninteractive sudo access through `/machine/sudo/info`.

The installed policy is authoritative. Moonraker's writable `moonraker.asvc` file
does not grant helper privileges or expand the reported service inventory.

## Compatibility

| Component | Target / verified version |
| --- | --- |
| Moonraker | **0.11.0**, including the bundled integration patch |
| GNU Shepherd | **0.10.5** (Guix package checks) and **1.0.9**, control protocol version 0 |
| Guile | 3.0; helper checks run with 3.0.11 |
| guile-json | 4.x; checked with 4.7.3 |
| Python | 3.9+; provider unit tests checked with 3.14, real Moonraker with 3.12 |
| Build backend | setuptools 77+ |

Moonraker 0.11.0 does not expose an external-provider registration hook. The
version-specific patch registers this package, recognizes Shepherd as PID 1,
and delegates the default sudo diagnostic to the provider. Actual Docker/LXC
cgroup detection is retained. The provider preserves Moonraker's virtualization
information; it does not discover hypervisors itself.

Other Moonraker and Shepherd versions require compatibility verification before
being added to this table. The Python package deliberately does not fetch
Moonraker from a package registry: the system package supplies it.

## Guix packaging

The Guix package is maintained in the separate
[spartan-guix](https://github.com/Idorobots/spartan-guix) channel, in
`(spartan packages moonraker-provider-shepherd)`. After adding that channel and
pulling, build the published provider with:

```sh
guix build moonraker-provider-shepherd
```

That package pins a provider release tag and source hash. It installs the Python
module, an executable Guile helper with its
interpreter/module closure, and the Moonraker patch under
`share/moonraker-provider-shepherd/patches/`. Its build runs the Python, Guile,
and private-Shepherd checks without root. The consuming Moonraker package
supplies Moonraker itself, avoiding a dependency cycle between the two packages.

## Moonraker integration contract

The downstream Moonraker package/service needs these build and configuration
steps:

1. Install the Python package into Moonraker's Python environment.
2. Apply `patches/moonraker-0.11.0-shepherd.patch` to Moonraker's source **before
   byte-compilation**:

   ```sh
   patch --batch --fuzz=0 -p1 < /path/to/moonraker-0.11.0-shepherd.patch
   ```

3. Use this Guix package's installed helper. For other installations, supply a
   system wrapper with an absolute Guile interpreter and trusted module paths
   for this module, Shepherd, and guile-json. The source launcher expects those
   module paths to be supplied by its installation environment.
4. Generate an immutable management policy and exact sudo rules from the same
   declaration.
5. Reference the installed helper and policy in Moonraker's configuration.

A wheel includes the helper sources, example policy, and compatibility patch
under `share/moonraker-provider-shepherd/`. Installing a wheel alone does not
configure root privileges or register the provider in Moonraker.

### Moonraker configuration

```ini
[machine]
provider: shepherd
validate_service: False
validate_config: False
shepherd_helper: /gnu/store/…-moonraker-provider-shepherd/bin/moonraker-shepherd
shepherd_policy: /gnu/store/…-moonraker-shepherd-policy.scm
shepherd_sudo: /run/privileged/bin/sudo
shepherd_timeout: 60
```

Replace the abbreviated store paths with actual build outputs. The helper and
sudo executable must have absolute paths. The policy must have an **absolute,
canonical path without symlinks**, so pass its store path directly rather than
an `/etc` symlink to it. It must be a regular file owned by the helper's effective
user (root under sudo), protected from group/other writes and replacement through
its ancestors. Root-owned sticky directories are permitted for temporary tests.

`shepherd_timeout` bounds the client wait for an operation. Timeout/cancellation
cleanup gets at most one additional second, or the configured timeout if shorter.
Inherited output handles in helper children do not delay that cleanup. A timeout
does not cancel an operation already submitted to Shepherd; inspect service
status before retrying. Status polling uses every second `proc_stats` callback,
normally about every two seconds. An initial helper failure leaves the API
running, reports a warning, and keeps polling so status can recover.

Disable Moonraker's install validators because Guix owns the configuration and
service definitions. Keep the update manager disabled as well.

### Management policy

See [`examples/policy.scm`](examples/policy.scm):

```scheme
((version . 1)
 (socket . "/var/run/shepherd/socket")
 (moonraker-service . "moonraker")
 (services . (("klipper" start stop restart)
              ("moonraker" restart)))
 (power . (poweroff reboot)))
```

This is read as data, not evaluated. Unknown fields, duplicate entries, invalid
service names, and unsupported actions are rejected. Names use ASCII letters,
digits, hyphens, and underscores. Additional instances can use names such as
`klipper-2` and `moonraker-2`.

- `services` lists visible service names and each one's permitted actions. An
  empty action list provides status-only access.
- `moonraker-service` identifies this Moonraker instance. It must occur in
  `services` and can only permit `restart`. The helper enforces this restriction
  for aliases of the same Shepherd service as well.
- `power` independently permits `poweroff`, `reboot`, and optionally `halt`.
  An empty list disables power operations. Moonraker's `shutdown_action` defaults
  to `poweroff`; set it to `halt` only if that action is also permitted.
- `socket` is fixed by the installed policy, not by a request or environment
  variable. The `root`/`shepherd` service and its aliases cannot be managed as
  ordinary services.

Shepherd still resolves service dependencies normally. In particular,
**Moonraker must not have a hard Shepherd requirement on Klipper**: stopping
Klipper would stop the API needed to start it again. The helper rejects stop or
restart requests that would transitively stop Moonraker through such a
dependency. Both daemons can start independently; Moonraker reconnects to Klippy
when its socket becomes available. Other declared services should also have
their dependency effects considered when assigning management rights.

### Sudo permissions

Each request executes an argument vector, without a shell:

```text
/run/privileged/bin/sudo -n -- HELPER POLICY COMMAND [SERVICE]
```

Generate exact command specifications for the permitted combinations. For
example, with full immutable paths substituted for `HELPER` and `POLICY`:

```sudoers
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY capabilities
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY status
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY start klipper
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY stop klipper
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY restart klipper
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY restart moonraker
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY poweroff
klipper ALL=(root) NOPASSWD: NOSETENV: HELPER POLICY reboot
```

Use sudo's default environment reset and trusted helper/module paths. Include the
rules in the operating system's validated `sudoers-file`, retaining its existing
rules. Guix's default sudoers specification does **not** include `sudoers.d`.

`/machine/sudo/info?check_access=true` checks the `capabilities` invocation. This
does not prove every individual sudoers rule exists; missing action rules fail
when that action is requested. No password prompt is used by this provider.

Moonraker-authorized clients, including clients on its trusted networks, can use
the permitted management endpoints. Choose those networks with the enabled
shutdown and service-control powers in mind.

### Restart and power semantics

Ordinary service operations wait for Shepherd. Starts and restarts also verify
the resulting state, because Shepherd can acknowledge a restart whose start
constructor subsequently fails.

Self-restart and power requests return **accepted**, then execute after a short
delay in a detached session. This allows the HTTP request to complete and avoids
the worker being killed with Moonraker's process group. Acceptance means queued,
not completed: detached failures are sent to syslog with the
`moonraker-shepherd` identifier. Confirm completion by reconnecting and observing
service status or the host's boot state.

Power-off sends `power-off root`, reboot sends `stop root`, and halt sends
`halt root`. These use Shepherd's orderly service shutdown path. Firmware restart,
emergency stop, and other printer commands remain Klipper API operations.

## Helper interface

```text
moonraker-shepherd POLICY capabilities
moonraker-shepherd POLICY status
moonraker-shepherd POLICY start|stop|restart SERVICE
moonraker-shepherd POLICY poweroff|halt|reboot
```

One JSON response is written to stdout. Diagnostics from the interpreter go to
stderr. Successful requests exit zero:

```json
{"protocol":1,"ok":true,"result":{"accepted":true}}
```

Failures exit nonzero:

```json
{"protocol":1,"ok":false,"error":{"code":"denied","message":"Power action is not permitted by policy"}}
```

`capabilities` returns the policy's `services`, `power`, and `moonraker_service`.
`status` returns a `services` object keyed by registered, declared names. Each
entry contains `active_state`, `sub_state`, `pid` (or null), `actions`, and
`properties` (`Requires` and `Description`). Connection failure is an error;
it is not reported as an empty service list. The Python provider reports known
services as unknown/unavailable while polling cannot reach Shepherd.

## Development and verification

The normal suite runs without root and creates its own private Shepherd socket.
Its power policy is empty. The helper unit tests simulate power operations.

```sh
guix shell python guile guile-json shepherd make -- make check
guix shell python python-setuptools -- make check-distribution
```

Alternatively, supply these dependencies through an existing environment. Set
`SHEPHERD=/absolute/path/to/shepherd` if it is not on `PATH`, and make Shepherd's
Guile modules available on `GUILE_LOAD_PATH` for the helper unit tests.

Test the patch and actual HTTP API using an existing Guix Moonraker 0.11.0 output:

```sh
SHEPHERD=/path/to/shepherd python3 tests/check_moonraker.py /gnu/store/…-moonraker-0.11.0
```

This copies Moonraker into a temporary writable tree, removes its cached
bytecode, applies the patch with zero fuzz, and runs the actual daemon under a
private Shepherd. It verifies service control, independent Klipper stop/start,
Moonraker self-restart through the HTTP API, and the sudo diagnostic. A test sudo
adapter exercises the command interface without granting privileges.

`check_distribution.py` builds a Git-free source archive, then builds a wheel
from that archive and verifies that the license, helper, policy example, and
patch are included. This build-content check uses Python 3.12+.

## GitHub Actions

`.github/workflows/check.yml` runs for pushes to `main`, pull requests, manual
dispatches, and release-workflow calls. It includes:

- Python unit tests on 3.9, 3.12, and 3.14.
- Helper and private-Shepherd integration tests, with dependencies supplied by
  the authenticated Guix commit in `ci/channels.scm`.
- Git-free source-archive and wheel-content checks.
- Real Moonraker 0.11.0 compatibility and HTTP API checks against a private
  Shepherd. The upstream archive is SHA-256 checked before dependency
  installation; these tests use a sudo adapter and require no Guix System VM.

`.github/workflows/release.yml` runs for pushed `v*` tags. It first calls the
complete test workflow, checks that the tag matches both Python version
declarations, builds the sdist and wheel, generates `SHA256SUMS`, and publishes a
GitHub release with generated notes and all three assets. It uses the repository's
`GITHUB_TOKEN`; only the publication job requests write permission.

For a release, update `pyproject.toml` and
`moonraker_provider_shepherd/__init__.py` together. Check them with:

```sh
python3 ci/check_version.py --tag v0.1.1
```

Commit and push the completed provider, then push the corresponding tag to
trigger publication. The current version is `0.1.1`, so its tag is `v0.1.1`.
Update the downstream package version and source hash in spartan-guix when
consuming a new release.

## Release contents

Version `0.1.1` is the current development version. All runtime sources and
compatibility patches live in this repository, and builds do not consult Git
metadata. A tagged GitHub source archive is sufficient for a pinned Guix source;
an sdist or wheel can additionally be attached to a release.

Push the provider commit and release tag before building its remote source from
spartan-guix. Downstream integration will generate the policy and sudoers rules,
apply the patch, and remove the hard Klipper dependency. Actual Guix System
power-operation testing remains a separate host-level check.

## License

This project's original code and integration changes are provided under the
[MIT license](LICENSE). Moonraker, Shepherd, Guile, and other dependencies retain
their respective licenses.
