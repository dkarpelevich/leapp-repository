import contextlib
import functools
import os
import re
import time
from collections import namedtuple

from leapp import reporting
from leapp.exceptions import StopActorExecutionError
from leapp.libraries.stdlib import CalledProcessError, api
from leapp.models import RHSMInfo

_RE_REPO_UID = re.compile(r'Repo ID:\s*([^\s]+)')
_RE_RELEASE = re.compile(r'Release:\s*([^\s]+)')
_RE_SKU_CONSUMED = re.compile(r'SKU:\s*([^\s]+)')
_ATTEMPTS = 5
_RETRY_SLEEP = 5
_DEFAULT_RHSM_REPOFILE = '/etc/yum.repos.d/redhat.repo'


def _rhsm_retry(max_attempts, sleep=None):
    """
    A decorator to retry executing a function/method if unsuccessful.

    The function/method execution is considered unsuccessful when it raises StopActorExecutionError.

    :param max_attempts: Maximum number of attempts to execute the decorated function/method.
    :param sleep: Time to wait between attempts. In seconds.
    """
    def impl(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            attempts = 0
            while True:
                attempts += 1
                try:
                    return f(*args, **kwargs)
                except StopActorExecutionError:
                    if max_attempts <= attempts:
                        api.current_logger().warning(
                            'Attempt %d of %d to perform %s failed. Maximum number of retries has been reached.',
                            attempts, max_attempts, f.__name__)
                        raise
                    if sleep:
                        api.current_logger().info(
                            'Attempt %d of %d to perform %s failed - Retrying after %s seconds',
                            attempts, max_attempts, f.__name__, str(sleep))
                        time.sleep(sleep)
                    else:
                        api.current_logger().info(
                            'Attempt %d of %d to perform %s failed - Retrying...', attempts, max_attempts, f.__name__)
        return wrapper
    return impl


@contextlib.contextmanager
def _handle_rhsm_exceptions(hint=None):
    """
    Context manager based function that handles exceptions of `run` for the subscription-manager calls.
    """
    try:
        yield
    except OSError as e:
        api.current_logger().error('Failed to execute subscription-manager executable')
        raise StopActorExecutionError(
            message='Unable to execute subscription-manager executable: {}'.format(str(e)),
            details={
                'hint': 'Please ensure subscription-manager is installed and executable.'
            }
        )
    except CalledProcessError as e:
        raise StopActorExecutionError(
            message='A subscription-manager command failed to execute',
            details={
                'details': str(e),
                'stderr': e.stderr,
                'hint': hint or 'Please ensure you have a valid RHEL subscription and your network is up.'
            }
        )


def skip_rhsm():
    """Check whether we should skip RHSM related code."""
    return os.getenv('LEAPP_DEVEL_SKIP_RHSM', '0') == '1'


def with_rhsm(f):
    """Decorator to allow skipping RHSM functions by executing a no-op."""
    if skip_rhsm():
        @functools.wraps(f)
        def _no_op(*args, **kwargs):
            return
        return _no_op
    return f


@with_rhsm
def get_attached_skus(context):
    """
    Retrieve the list of the SKUs the system is attached to with the subscription-manager.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :return: SKUs the current system is attached to.
    :rtype: List(string)
    """
    with _handle_rhsm_exceptions():
        result = context.call(['subscription-manager', 'list', '--consumed'], split=False)
        return _RE_SKU_CONSUMED.findall(result['stdout'])


def get_available_repo_ids(context, releasever=None):
    """
    Retrieve repo ids of all the repositories available through the subscription-manager.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :param releasever: Release version to pass to the `yum repoinfo` command
    :type releasever: string
    :return: Repositories that are available to the current system through the subscription-manager
    :rtype: List(string)
    """
    cmd = ['yum', 'repoinfo']
    if releasever:
        cmd.extend(['--releasever', releasever])
    try:
        result = context.call(cmd)
    except CalledProcessError as exc:
        raise StopActorExecutionError(
            'Unable to get list of available yum repositories.',
            details={'details': str(exc), 'stderr': exc.stderr}
        )
    _inhibit_on_duplicate_repos(result['stderr'])
    available_repos = list(_get_repos(result['stdout']))
    available_rhsm_repos = [repo.repoid for repo in available_repos if repo.file == _DEFAULT_RHSM_REPOFILE]
    list_separator_fmt = '\n    - '
    if available_rhsm_repos:
        api.current_logger().info('The following repoids are available through RHSM:{0}{1}'
                                  .format(list_separator_fmt, list_separator_fmt.join(available_rhsm_repos)))
    else:
        api.current_logger().info('There are no repos available through RHSM.')
    return available_rhsm_repos


def _inhibit_on_duplicate_repos(repos_raw_stderr):
    """
    Inhibit the upgrade if any repoid is defined multiple times.

    When that happens, it not only shows misconfigured system, but then we can't get details of all the available
    repos as well.
    """
    duplicates = []
    for duplicate in re.findall(
            r'Repository ([^\s]+) is listed more than once', repos_raw_stderr, re.DOTALL | re.MULTILINE):
        duplicates.append(duplicate)

    if not duplicates:
        return
    list_separator_fmt = '\n    - '
    api.current_logger().warn('The following repoids are defined multiple times:{0}{1}'
                              .format(list_separator_fmt, list_separator_fmt.join(duplicates)))

    reporting.create_report([
        reporting.Title('A YUM/DNF repository defined multiple times'),
        reporting.Summary(
            'The `yum repoinfo` command reports that the following repositories are defined multiple times:{0}{1}'.
            format(list_separator_fmt, list_separator_fmt.join(duplicates))
        ),
        reporting.Severity(reporting.Severity.MEDIUM),
        reporting.Tags([reporting.Tags.REPOSITORY]),
        reporting.Flags([reporting.Flags.INHIBITOR]),
        reporting.Remediation(hint='Remove the duplicit repository definitions.')
    ])


def _get_repos(repos_stdout_raw):
    """
    Generator providing all the repos available through yum/dnf.

    :rtype: Iterator[:py:class:`leapp.libraries.common.rhsm.Repo`]
    """
    # Split all the available repos per one repo
    for repo_params_raw in re.findall(
            r"Repo-id.*?Repo-filename.*?\n", repos_stdout_raw, re.DOTALL | re.MULTILINE):
        yield _parse_repo_params(repo_params_raw)


def _parse_repo_params(repo_params_raw):
    """Parse multiline string holding repo parameters to distill the important ones."""
    try:
        repoid = _get_repo_param(r'^Repo-id\s+:\s+([^/]+?)(/.*?)?$', repo_params_raw, 'Repo-id')
        repofile = _get_repo_param(r'^Repo-filename:\s+(.*?)$', repo_params_raw, 'Repo-filename')
        return namedtuple('Repository', ['repoid', 'file'])(repoid, repofile)
    except ValueError as err:
        err_detail = ("Failed to parse the '{0}' repo parameter within the following part of the"
                      " `yum repoinfo` output:\n{1}".format(err.args[0], repo_params_raw))
        raise StopActorExecutionError(
            message='Failed to parse the `yum repoinfo` output',
            details={'details': err_detail})


def _get_repo_param(pattern, repo_params_raw, param):
    """Parse a string with all the repo params to get the value of a single repo param."""
    repo_param = re.search(pattern, repo_params_raw, re.MULTILINE | re.DOTALL)
    if repo_param:
        return repo_param.group(1)
    raise ValueError(param, repo_params_raw)


@with_rhsm
def get_enabled_repo_ids(context):
    """
    Retrieve repo ids of all the repositories enabled through the subscription-manager.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :return: Repositories that are enabled on the current system through the subscription-manager.
    :rtype: List(string)
    """
    with _handle_rhsm_exceptions():
        result = context.call(['subscription-manager', 'repos', '--list-enabled'], split=False)
        return _RE_REPO_UID.findall(result['stdout'])


@with_rhsm
@_rhsm_retry(max_attempts=_ATTEMPTS, sleep=_RETRY_SLEEP)
def unset_release(context):
    """
    Unset the configured release from the subscription-manager.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    """
    with _handle_rhsm_exceptions():
        context.call(['subscription-manager', 'release', '--unset'], split=False)


@with_rhsm
@_rhsm_retry(max_attempts=_ATTEMPTS, sleep=_RETRY_SLEEP)
def set_release(context, release):
    """
    Set the release (RHEL minor version) through the subscription-manager.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :param release: Release to set the subscription-manager to.
    :type release: str
    """
    with _handle_rhsm_exceptions():
        context.call(['subscription-manager', 'release', '--set', release], split=False)


@with_rhsm
def get_release(context):
    """
    Retrieves the release the subscription-manager has been pinned to, if applicable.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :return: Release the subscription-manager is set to.
    :rtype: string
    """
    with _handle_rhsm_exceptions():
        result = context.call(['subscription-manager', 'release'], split=False)
        result = _RE_RELEASE.findall(result['stdout'])
        return result[0] if result else ''


@with_rhsm
@_rhsm_retry(max_attempts=_ATTEMPTS, sleep=_RETRY_SLEEP)
def refresh(context):
    """
    Calls 'subscription-manager refresh'

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    """
    with _handle_rhsm_exceptions():
        context.call(['subscription-manager', 'refresh'], split=False)


@with_rhsm
def get_existing_product_certificates(context):
    """
    Retrieves information about existing product certificates on the system.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :return: Paths to product certificates that are currently installed on the system.
    :rtype: List(string)
    """
    certs = []
    for path in ('/etc/pki/product', '/etc/pki/product-default'):
        if not os.path.isdir(context.full_path(path)):
            continue
        curr_certs = [os.path.join(path, f) for f in os.listdir(context.full_path(path))
                      if os.path.isfile(os.path.join(context.full_path(path), f))]
        if curr_certs:
            certs.extend(curr_certs)
    return certs


@with_rhsm
def set_container_mode(context):
    """
    Put RHSM into the container mode.

    Inside the container, we have to ensure the RHSM is not used AND that host
    is not affected. If the RHSM is not set into the container mode, the host
    could be affected and the generated repo file in the container could be
    affected as well (e.g. when the release is set, using rhsm, on the host).

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    """
    if not context.is_isolated():
        api.current_logger().error('Trying to set RHSM into the container mode'
                                   'on host. Skipping the action.')
        return
    try:
        context.call(['ln', '-s', '/etc/rhsm', '/etc/rhsm-host'])
    except CalledProcessError:
        raise StopActorExecutionError(
                message='Cannot set the container mode for the subscription-manager.')


@with_rhsm
def switch_certificate(context, rhsm_info, cert_path):
    """
    Perform all actions needed to switch the passed RHSM product certificate.

    This function will copy the certificate to /etc/pki/product, and /etc/pki/product-default if necessary, and
    remove other product certificates from there.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :param rhsm_info: An instance of the RHSMInfo model
    :type rhsm_info: RHSMInfo model
    :param cert_path: Path to the product certificate to switch to
    :type cert_path: string
    """
    for existing in rhsm_info.existing_product_certificates:
        try:
            context.remove(existing)
        except OSError:
            api.current_logger().warn('Failed to remove existing certificate: %s', existing, exc_info=True)

    for path in ('/etc/pki/product', '/etc/pki/product-default'):
        if os.path.isdir(context.full_path(path)):
            context.copy_to(cert_path, os.path.join(path, os.path.basename(cert_path)))


@with_rhsm
def scan_rhsm_info(context):
    """
    Gather all the RHSM information of the source system.

    It's not intended for gathering RHSM info about the target system within a container.

    :param context: An instance of a mounting.IsolatedActions class
    :type context: mounting.IsolatedActions class
    :return: An instance of an RHSMInfo model.
    :rtype: RHSMInfo model
    """
    info = RHSMInfo()
    info.attached_skus = get_attached_skus(context)
    info.available_repos = get_available_repo_ids(context)
    info.enabled_repos = get_enabled_repo_ids(context)
    info.release = get_release(context)
    info.existing_product_certificates.extend(get_existing_product_certificates(context))
    return info
