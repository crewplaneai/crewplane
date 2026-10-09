from crewplane.artifacts.generated_files import io_windows
from crewplane.artifacts.generated_files.io import GeneratedFileOperations


def local_windows_generated_operations() -> GeneratedFileOperations:
    return GeneratedFileOperations(
        io_windows.copy_generated_file_snapshot_candidate,
        io_windows.copy_generated_result,
        io_windows.reset_directory,
        io_windows.prepare_directory,
    )
