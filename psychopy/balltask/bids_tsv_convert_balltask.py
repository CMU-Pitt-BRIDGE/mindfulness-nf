'''
Paul Alexander Bloom
July 17 2023

Runs upon exit of balltask to convert the csv output to a BIDS-compatible tsv file

'''

import os
import pandas as pd
import numpy as np

def convert_balltask_csv_to_bids(infile):
    # block_duration=28
    # response_duration=1
    # presentation_duration=2.5
    # block_type_instruction_duration=2
    # Defensive: when the roi_outputs filename uses the task-based pattern
    # (e.g. ``..._DMN_transferpre_1_roi_outputs.csv``) the sibling slider
    # csv follows the same template. The ``.replace`` below handles both
    # the old ``_DMN_Feedback_`` filename style and the new task-based one,
    # because it only swaps the ``roi_outputs`` token.
    slider_outputs = pd.read_csv(infile.replace('roi_outputs', 'slider_questions'))
    slider_outputs = slider_outputs[-slider_outputs.run.isna()]
    slider_outputs.reset_index(inplace=True)
    df = pd.read_csv(infile)
    df.rename(columns = {
                    'time':'onset',
                    'stage':'trial_type',
                    'cen':'cen_signal',
                    'dmn':'dmn_signal',
                    'volume':'feedback_source_volume'}, 
              inplace=True)
    # BIDS events: duration is in seconds. Previously hardcoded to 0, which
    # violates BIDS (zero-duration events are only valid for strictly
    # instantaneous markers). These rows represent per-TR samples of
    # streamed cen/dmn signal, so duration = one TR = 1.2 s. Downstream
    # GLM tools that expect BIDS-compliant events will now accept this.
    _tr_seconds = 1.2
    df['duration'] = _tr_seconds
    df['pda']=df.cen_signal-df.dmn_signal
    df['cen_hit']=np.where(df.cen_cumulative_hits.diff(periods=-1) == -1, 1, 0)
    df['dmn_hit']=np.where(df.dmn_cumulative_hits.diff(periods=-1) == -1, 1, 0)
    # Participant label: use the raw id as-is if it already carries the
    # 'sub-' prefix (e.g. 'sub-morgan'); only prepend if needed. Legacy
    # code unconditionally concatenated "sub-" + id, which produced
    # 'sub-sub-morgan' in the `participant` column of every TSV for
    # subjects whose id already had the prefix — corrupts any group-level
    # join. The filename generation (below) already handled this; the
    # column was missed. Regression confirmed on sub-morgan 2026-04-21.
    raw_id_col = str(slider_outputs['id'][0])
    df['participant'] = raw_id_col if raw_id_col.startswith('sub-') else f"sub-{raw_id_col}"
    df['run'] = slider_outputs['run'][0]
    df['feedback_on'] = slider_outputs['feedback_on'][0]
    df['slider_noting'] = (slider_outputs.loc[slider_outputs.question_text=='How often were you using the mental noting practice?', 'response'])
    df['slider_ballcheck'] = (slider_outputs.loc[slider_outputs.question_text=='How often did you check the position of the ball?', 'response'])
    df['slider_difficulty'] = (slider_outputs.loc[slider_outputs.question_text=='How difficult was it to apply mental noting?', 'response'])
    df['slider_calm'] = (slider_outputs.loc[slider_outputs.question_text=='How calm do you feel right now?', 'response'])
    df = df.astype(object).fillna('n/a')
    out_df = df[['onset', 'duration', 'trial_type', 'feedback_source_volume',
                 'cen_signal', 'dmn_signal', 'pda', 
                 'ball_y_position','cen_hit', 'dmn_hit', 
                'scale_factor', 'participant', 'run', 'feedback_on',
                'slider_noting', 'slider_ballcheck', 'slider_difficulty', 'slider_calm']]

    run_num = int(slider_outputs['run'][0])

    # Task + session come from the orchestrator via env vars so different
    # sessions (rt15, rt30) for the same subject don't collide on the same
    # filename. Legacy inference (run==1 → transferpre, run==2 → transferpost)
    # was wrong for our rt15 protocol: Feedback 1 is run=2 and Transfer Post
    # is run=7. Env var is authoritative; fallback only exists so standalone
    # ad-hoc runs keep working.
    env_task = os.environ.get('MINDFULNESS_NF_TASK')
    if env_task:
        run_type = env_task
    else:
        # Legacy fallback (kept for standalone invocations).
        if str(slider_outputs['feedback_on'][0]) == 'Feedback':
            run_type = 'feedback'
        else:
            if run_num == 1:
                run_type = 'transferpre'
            elif run_num == 2:
                run_type = 'transferpost'
                run_num = 1
            elif run_num == 3:
                run_type = 'transferpost'
                run_num = 2
            else:
                run_type = 'unknown'

    # Session label: orchestrator-supplied via env; fall back to the legacy
    # 'nf' literal for standalone runs.
    ses_label = os.environ.get('MINDFULNESS_NF_SESSION_TYPE', 'nf')

    # put together bids tsv filename. Participant id may already carry the
    # 'sub-' prefix (e.g. 'sub-process-rehearse'); unconditionally prepending
    # 'sub-' produced 'sub-sub-<name>_...' for those. Strip the prefix first.
    raw_id = str(slider_outputs['id'][0])
    subject_label = raw_id[4:] if raw_id.startswith('sub-') else raw_id
    # Root under the orchestrator-supplied data dir (session-scoped) when
    # present; fall back to legacy sibling ``data/`` for standalone runs.
    data_root = os.environ.get('MINDFULNESS_NF_PSYCHOPY_DATA_DIR', 'data')
    outdir = os.path.join(data_root, raw_id)
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.join(
        outdir,
        'sub-' + subject_label
        + '_ses-' + ses_label
        + '_task-' + run_type
        + '_run-' + "{:02d}".format(run_num)
        + '.tsv'
    )
    out_df.to_csv(outfile, sep ='\t', index=False)
    return(out_df)
