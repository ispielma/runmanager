from .client import *
from .client import RunmanagerClient as Client

# Built here, where user code imports it, so that importing runmanager.client
# builds no client:
_default_client = RunmanagerClient()

say_hello = _default_client.say_hello
get_version = _default_client.get_version
get_values = _default_client.get_values
get_globals = _default_client.get_globals
set_values = _default_client.set_values
set_globals = _default_client.set_globals
get_scans = _default_client.get_scans
set_scans = _default_client.set_scans
get_scan_enabled = _default_client.get_scan_enabled
set_scan_enabled = _default_client.set_scan_enabled
get_jit_enabled = _default_client.get_jit_enabled
set_jit_enabled = _default_client.set_jit_enabled
engage = _default_client.engage
abort = _default_client.abort
get_run_shots = _default_client.get_run_shots
set_run_shots = _default_client.set_run_shots
get_view_shots = _default_client.get_view_shots
set_view_shots = _default_client.set_view_shots
get_shuffle = _default_client.get_shuffle
set_shuffle = _default_client.set_shuffle
n_shots = _default_client.n_shots
get_labscript_file = _default_client.get_labscript_file
set_labscript_file = _default_client.set_labscript_file
get_shot_output_folder = _default_client.get_shot_output_folder
set_shot_output_folder = _default_client.set_shot_output_folder
error_in_globals = _default_client.error_in_globals
is_output_folder_default = _default_client.is_output_folder_default
reset_shot_output_folder = _default_client.reset_shot_output_folder
shot_status = _default_client.shot_status
submit_shots = _default_client.submit_shots
queue_exchange = _default_client.queue_exchange

if __name__ == '__main__':
    # Test
    import time

    current = get_values()
    print("get globals:", current)
    print("set globals", set_values({'test': current['test']}, raw=True))
    assert get_values()['test'] == current['test']
    engage()
