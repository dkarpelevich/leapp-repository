import os

from leapp.libraries.actor import cpu
from leapp.libraries.common import testutils
from leapp.libraries.stdlib import api
from leapp.models import CPUInfo


class mocked_get_cpuinfo(object):
    def __init__(self, filename):
        self.filename = filename

    def __call__(self):
        """
        Return lines of the self.filename test file located in the files directory.

        Those files contain /proc/cpuinfo content from several machines.
        """
        with open(os.path.join('tests/files', self.filename), 'r') as fp:
            return fp.readlines()


def test_machine_type(monkeypatch):
    # cpuinfo doesn't contain a machine field
    mocked_cpuinfo = mocked_get_cpuinfo('cpuinfo_x86_64')
    monkeypatch.setattr(cpu, '_get_cpuinfo', mocked_cpuinfo)
    monkeypatch.setattr(api, 'produce', testutils.produce_mocked())
    cpu.process()
    assert api.produce.called == 1
    assert CPUInfo() == api.produce.model_instances[0]

    # cpuinfo contains a machine field
    api.produce.called = 0
    api.produce.model_instances = []
    mocked_cpuinfo.filename = 'cpuinfo_s390x'
    cpu.process()
    assert api.produce.called == 1
    assert CPUInfo(machine_type=2827) == api.produce.model_instances[0]
