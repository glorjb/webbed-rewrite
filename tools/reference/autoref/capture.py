"""Session files in exactly the layout written by webbed_probe.py v3, so tests/capture_lib.py and every
existing regression test read automated recordings unchanged:

  <session>/segment_01.jsonl        one fast-lane row per game frame (probe row schema)
  <session>/segment_01_webs.jsonl   web records (focus / recent lanes every frame, sweep lane in idle time)
  <session>/session.json            probe metadata + per-segment statistics (same keys as the probe)

Runner-only data goes to files the capture loader ignores (it globs `segment_*.jsonl`):
  <session>/inputs.jsonl            every injected input, its oIO acknowledgement and aim / state events
  <session>/auto_run.json           scenario result, failure reason, measurements, quality summary
"""
import datetime
import json
import os
import time


def _dump(obj):
    return json.dumps(obj, separators=(',', ':'), default=str)


class SessionWriter:
    def __init__(self, path, meta_extra=None, clock=time.perf_counter):
        if os.path.exists(path):
            raise FileExistsError('refusing to write into an existing capture directory: %s' % path)
        os.makedirs(path)
        self.path = path
        self.clock = clock
        self.t0 = clock()
        self.meta = {'build': None, 'sha256': None, 'started': datetime.datetime.now().isoformat(),
                     'probe': 'auto_runner (webbed_probe.py v3 reads: read-only polling, fast/slow lanes)',
                     'segments': []}
        self.meta.update(meta_extra or {})
        self.seg = 0
        self.f = self.fw = None
        self.marker = 0
        self.last_rec_frame = None
        self.inputs = open(os.path.join(path, 'inputs.jsonl'), 'w')

    @property
    def recording(self):
        return self.f is not None

    # ---------------------------------------------------------------- segments (mirrors probe start/stop_segment)
    def start_segment(self):
        self.seg += 1
        self.marker = 0
        base = os.path.join(self.path, 'segment_%02d' % self.seg)
        self.f = open(base + '.jsonl', 'w')
        self.fw = open(base + '_webs.jsonl', 'w')
        self.last_rec_frame = None
        self.meta['segments'].append({
            'file': os.path.basename(base) + '.jsonl', 'webs_file': os.path.basename(base) + '_webs.jsonl',
            'rows': 0, 'rows_with_player': 0, 'skipped_frames': 0, 'torn_rows': 0, 'web_records': 0,
            'web_sweeps': 0, 'first_frame': None, 'last_frame': None, 'fast_ms_max': 0.0,
            'started_t': self.clock() - self.t0})

    def stop_segment(self):
        if not self.recording:
            return None
        self.f.close()
        self.fw.close()
        self.f = self.fw = None
        sg = self.meta['segments'][-1]
        sg['duration_s'] = round(self.clock() - self.t0 - sg['started_t'], 3)
        span = (sg['last_frame'] - sg['first_frame'] + 1) if sg['rows'] else 0
        sg['frame_span'] = span
        sg['coverage'] = round(sg['rows'] / span, 4) if span else 0.0
        sg['effective_hz'] = round(sg['rows'] / sg['duration_s'], 2) if sg['duration_s'] else 0.0
        return sg

    def write_row(self, row, focus):
        """One fast-lane row (already carrying torn / frame_start / fast_ms) and its focus web records."""
        self.f.write(_dump(row) + '\n')
        for r in focus:
            self.fw.write(_dump(r) + '\n')
        sg = self.meta['segments'][-1]
        sg['rows'] += 1
        sg['rows_with_player'] += 1 if row['player'] and row['player'].get('body') else 0
        sg['skipped_frames'] += row['skipped']
        sg['torn_rows'] += row['torn']
        sg['web_records'] += len(focus)
        sg['shot_frames'] = sg.get('shot_frames', 0) + (1 if row.get('shots') else 0)
        sg['shot_missing_frames'] = sg.get('shot_missing_frames', 0) + row.get('shot_missing', 0)
        sg['new_instances'] = sg.get('new_instances', 0) + len(row.get('new_instances') or [])
        sg['first_frame'] = sg['first_frame'] if sg['first_frame'] is not None else row['frame']
        sg['last_frame'] = row['frame']
        sg['fast_ms_max'] = max(sg['fast_ms_max'], row.get('fast_ms', 0.0))
        self.last_rec_frame = row['frame']

    def write_sweep(self, rec):
        self.fw.write(_dump(rec) + '\n')
        self.meta['segments'][-1]['web_records'] += 1

    def sweep_done(self):
        if self.recording:
            self.meta['segments'][-1]['web_sweeps'] += 1

    def log(self, event):
        event = dict(event)
        event.setdefault('t', round(self.clock() - self.t0, 6))
        self.inputs.write(_dump(event) + '\n')

    # ---------------------------------------------------------------- close
    def close(self, auto_result=None):
        self.stop_segment()
        self.inputs.close()
        self.meta['total_rows_with_player'] = sum(s['rows_with_player'] for s in self.meta['segments'])
        with open(os.path.join(self.path, 'session.json'), 'w') as f:
            json.dump(self.meta, f, indent=1)
        if auto_result is not None:
            self.write_result(auto_result)

    def write_result(self, auto_result):
        with open(os.path.join(self.path, 'auto_run.json'), 'w') as f:
            json.dump(auto_result, f, indent=1, default=str)
