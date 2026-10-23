#ifndef NGX_SNAPSHOT_DIRECTORY_H
#define NGX_SNAPSHOT_DIRECTORY_H

#include <filesystem>

// Strip trailing separators/dots without resolving symlinks before O_NOFOLLOW.
inline std::filesystem::path SnapshotDirectoryPath(std::filesystem::path directory)
{
    while (directory.has_relative_path() &&
           (directory.filename().empty() || directory.filename() == ".")) {
        directory = directory.parent_path();
        if (directory.empty())
            return ".";
    }
    return directory;
}

#endif
