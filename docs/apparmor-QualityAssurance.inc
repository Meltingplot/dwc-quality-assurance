    # QualityAssurance, process data of every print job
    # (dwc-quality-assurance docs/image.md §2; scripts/sideload.sh loads this same file for tests)
    /opt/dsf/plugins/QualityAssurance.json r,
    /opt/dsf/plugins/QualityAssurance/** mr,
    owner /opt/dsf/plugins/QualityAssurance/dsf/__pycache__/ w,
    owner /opt/dsf/plugins/QualityAssurance/dsf/__pycache__/* rw,
    # SQLite database with WAL/SHM files and locks, backups, settings, G-code layer
    # index cache, timelapse frames and videos, temporary files
    /opt/dsf/sd/QualityAssurance/ rw,
    /opt/dsf/sd/QualityAssurance/** rwk,
    # job files: CRC32, slicer settings at the end of the file, toolpath for the replay
    /opt/dsf/sd/gcodes/ r,
    /opt/dsf/sd/gcodes/** r,
    # its HTTP endpoints (one of them is the WebSocket live): DuetControlServer creates the
    # namespace directory, QA binds one socket per endpoint in it
    /run/dsf/QualityAssurance/{,**/} r,
    /run/dsf/QualityAssurance/** rw,
    # bed calibration: the height map G29 wrote or loaded (move.compensation.file; RRF puts a name
    # without a directory into 0:/sys, GCodes6.cpp LoadHeightMap/MakeSysFileName, 3.7-dev @ 32a84d2)
    /opt/dsf/sd/sys/*.csv r,
    # accelerometer: read and delete the CSVs of its own M956 recordings (qa-*.csv); RRF writes
    # them through DSF, so the plugin never creates files there
    /opt/dsf/sd/sys/accelerometer/ r,
    /opt/dsf/sd/sys/accelerometer/qa-*.csv rw,
    # timelapse: the camera snapshot over HTTP (http://10.42.0.1/snapshot on the CHX 350; AppArmor
    # cannot narrow this to one address, so it holds for every bundled plugin) and ffmpeg/ffprobe
    # for encoding, frame extraction and the frame count, inheriting this profile
    network inet stream,
    /usr/bin/ffmpeg ix,
    /usr/bin/ffprobe ix,
