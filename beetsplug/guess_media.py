"""
Based on bitdepths/samplerates and (EAC/XLD) log-files,
determine if the media is likely a CD or likely not a CD.

Additionally checks the log files for a TOC,
and tries to get the release ids from musicbrainz.
"""

from beets import ui

from beets.plugins import BeetsPlugin
from beets.autotag import TrackInfo
from beets.autotag import hooks
import os.path
import re
from glob import glob
from collections import namedtuple
import io
import musicbrainzngs

MatchData = namedtuple('MatchData', ['has_log', 'ids'])
_matches = {}  # dict: key = dirname, value = MatchData


def _get_toc_string_from_log(file_handle):
    """
    Returns a toc string or None for a given log file (EAC or XLD)
    Copyright (c) 2018 Konstantin Mochalov
    Released under the MIT License
    Original source: https://gist.github.com/kolen/765526
    """
    def _filter_toc_entries(file_handle):
        """
        Take file handle, return iterator of toc entries
        """
        while True:
            line = file_handle.readline()
            # TOC table header:
            if re.match(r""" \s*
                       .+\s+ \| (?#track)
                    \s+.+\s+ \| (?#start)
                    \s+.+\s+ \| (?#length)
                    \s+.+\s+ \| (?#start sec)
                    \s+.+\s*$   (?#end sec)
                    """, line, re.X):
                file_handle.readline()
                break

        while True:
            line = file_handle.readline()
            m = re.match(r"""
                ^\s*
                (?P<num>\d+)
                \s*\|\s*
                (?P<start_time>[0-9:.]+)
                \s*\|\s*
                (?P<length_time>[0-9:.]+)
                \s*\|\s*
                (?P<start_sector>\d+)
                \s*\|\s*
                (?P<end_sector>\d+)
                \s*$
                """, line, re.X)
            if not m:
                break
            yield m.groupdict()

    PREGAP = 150
    try:
        entries = list(_filter_toc_entries(file_handle))
        num_entries = len(entries)

        tracknums = [int(e['num']) for e in entries]
        if [x for x in range(1, num_entries+1)] != tracknums:
            # Non-standard track number sequence
            return None

        leadout_offset = int(entries[-1]['end_sector']) + PREGAP + 1
        offsets = [(int(x['start_sector']) + PREGAP) for x in entries]
        toc_numbers = [1, num_entries, leadout_offset] + offsets
        return " ".join(str(x) for x in toc_numbers)
    except Exception as e:
        # can fail if the log file is malformed
        print("Ignoring log file because of the following error:")
        print(e)
        pass
    return None


def _get_releases_from_toc(toc):
    """Returns a list of musicbrainz release IDs from a toc string"""
    res = musicbrainzngs.get_releases_by_discid(id="", toc=toc)
    if res['release-list']:
        return [release['id'] for release in res['release-list']]


def _parse_logfile(filename):
    """
    Given a filename, parses a XLD/EAC log file.
    Returns a list of musicbrainz-IDs (or an empty list) if possible,
    otherwise None.
    """

    eac_regex = re.compile(r'Exact Audio Copy*')
    xld_regex = re.compile(r'X Lossless Decoder*')

    def _read_and_match(file_handle):
        line = file_handle.readline()
        if eac_regex.match(line) or xld_regex.match(line):
            toc = _get_toc_string_from_log(file_handle)
            ids = set(_get_releases_from_toc(toc)) if toc else []
            #print((toc,ids))
            return ids
        return None

    try:
        try:
            with io.open(filename, encoding='utf-8') as f:
                return _read_and_match(f)
        except UnicodeDecodeError:
            with io.open(filename, encoding='utf-16') as f:
                return _read_and_match(f)
    except Exception as e:
        pass
    return None


def _process_items(items):
    """Checks for valid logfiles, extracts TOC if possible,
    and adds the results to the global dict.

    Returns a set of musicbrainz-IDs if a valid log file was found,
    otherwise None.
    """
    paths = set(map(lambda item: os.path.dirname(item.path), items))
    ids = set()
    log_found = False

    for path in paths:
        matchdata_has_log = False
        matchdata_ids = set()
        if path not in _matches:
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    if not filename.lower().endswith(b'.log'):
                        continue
                    log_ids = _parse_logfile(os.path.join(dirpath, filename))
                    if not log_ids:
                        continue
                    matchdata_has_log = True
                    matchdata_ids.update(log_ids)

            # Add result for current path to global dict
            _matches[path] = MatchData(has_log=matchdata_has_log,
                                       ids=matchdata_ids)

        if _matches[path].has_log:
            ids.update(_matches[path].ids)
            log_found = True

    return ids if log_found else None


class GuessMedia(BeetsPlugin):
    def __init__(self):
        super(GuessMedia, self).__init__()
        self.config.add({
            'media_weight': 1.0,
            'album_id_weight': 1.0,
        })
        self.register_listener('import_task_start', self.import_task_start)

    def import_task_start(self, task, session):
        items = task.items if task.is_album else [task.item]
        _process_items(items)

    def candidates(self, items, artist, album, va_likely):
        releases = []
        release_ids = _process_items(items)
        if not release_ids:
            return releases
        for id in release_ids:
            try:  # album_for_mbid may raise a MusicBrainzAPIError
                albuminfo = hooks.album_for_mbid(id)
                if albuminfo:
                    releases.append(albuminfo)
            except:
                pass
        return releases

    def album_distance(self, items, album_info, mapping):
        dist = hooks.Distance()

        # check if the album has a log file (from EAC/XLD):
        release_ids = _process_items(items)
        has_log = release_ids is not None

        # get bitdepths and samplerates
        bitdepths = set(map(lambda item: item.bitdepth, items))
        samplerates = set(map(lambda item: item.samplerate, items))

        # Boolean flags to determine media type
        is_not_cd = max(bitdepths) > 16 or max(samplerates) != 44100
        could_be_cd = not is_not_cd
        candidate_media_is_cd = \
            "CD" in album_info.media.upper() if album_info.media else False

        # penalty for CDs if it's clearly not a CD
        if is_not_cd and candidate_media_is_cd:
            dist.add('media', self.config['media_weight'].as_number())
            album_info.data_source+='+' + ui.colorize('text_warning', 'guess_media_NOT_A_CD')

        # penalty if we think it's a CD (found a log file and
        # bitdepths/samplerates are correct) but the candidate media is wrong:
        if has_log and could_be_cd and not candidate_media_is_cd:
            dist.add('media', self.config['media_weight'].as_number())
            album_info.data_source+='+' + ui.colorize('text_warning', 'guess_media_IS_A_CD')

        # penalty if we found an album id from the log file,
        # and the album id does not match:
        if release_ids is not None and album_info.album_id not in release_ids:
            dist.add('album_id', self.config['album_id_weight'].as_number())
            album_info.data_source+='+' + ui.colorize('text_warning', 'guess_media_ALBUM_ID_FROM_LOG_WRONG')

        return dist
