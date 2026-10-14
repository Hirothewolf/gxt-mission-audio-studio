"""Clear the old dialogue under replacement takes without stretching either voice."""
import copy


def render_project(project):
    frozen = copy.deepcopy(project)
    if not frozen.data.get('scene'):
        return frozen
    active = {t['id'] for t in frozen.data['tracks'] if not t['mute']}
    solo = {t['id'] for t in frozen.data['tracks'] if t['solo'] and not t['mute']}
    if solo:
        active = solo
    ranges = []
    for clip in frozen.data['clips']:
        if clip['track_id'] not in active or frozen.track(clip['track_id'])['role'] != 'pt':
            continue
        first = clip['position']
        last = first + frozen.clip_frames(clip)
        end = clip.get('metadata', {}).get('replace_end')
        start = clip.get('metadata', {}).get('replace_start')
        if start is not None:
            first = min(first, round(float(start) * frozen.sample_rate))
        if end is not None:
            last = max(last, round(float(end) * frozen.sample_rate))
        ranges.append((first, last))
    if not ranges:
        return frozen
    retained = []
    for clip in frozen.data['clips']:
        if not clip.get('metadata', {}).get('scene_background') or clip['track_id'] not in active:
            retained.append(clip)
            continue
        # Split through the project's frame-exact operation. It retains sample
        # interpolation/fade envelopes even when source and project rates differ.
        work = copy.deepcopy(frozen)
        work.data['clips'] = [copy.deepcopy(clip)]
        boundaries = sorted({b for pair in ranges for b in pair})
        for boundary in boundaries:
            crossing = next((c for c in work.data['clips'] if c['position'] < boundary < c['position'] + work.clip_frames(c)), None)
            if crossing:
                work.track(crossing['track_id'])['locked'] = False
                work.split(crossing['id'], boundary)
        retained.extend(c for c in work.data['clips'] if not any(
            c['position'] < end and c['position'] + work.clip_frames(c) > start for start, end in ranges))
    frozen.data['clips'] = retained
    frozen.validate()
    return frozen
