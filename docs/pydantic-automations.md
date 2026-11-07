# Automations

An automation sends a saved prompt to one of your conversations on a fixed
interval. ModelMesh supports interval schedules only. It has no webhooks,
calendars or event triggers.

## Use

Open **Settings → Automations** and choose **New automation**.

| Field | Rule |
|---|---|
| Name | 1 to 150 characters |
| Instructions | The prompt, 1 to 100,000 characters |
| Session | One of your own active private web sessions |
| Model | A fixed model from the list. Auto is not offered |
| Repeat every (minutes) | 5 to 525,600 |

Each run adds an ordinary turn to the chosen session, with the automation's
model, as you. The turn follows the session's normal rules: it uses the
session's computer, and it can be stopped like any other turn.

- **Pause** and **Resume** change the schedule. Either one also records your
  current sign-in state for the automation's later runs.
- **Run history** lists the last 100 runs with their status and turn.
- The trash button deletes the automation. Future runs stop. Turns already
  started, and the session's history, stay.
- Schedules are private to their owner. If the owner's sign-in is revoked
  (an administrator disables or edits the account), later runs are denied.
- Schedules live in the local Temporal server's database (`temporal.db` in the
  data volume). They run only while ModelMesh runs. A run missed while
  ModelMesh was down is made up only if ModelMesh is back within 5 minutes.
- Creating an automation needs a running worker. If none is registered, the
  page says so and you can still pause or delete existing automations.

## How it runs

1. Saving an automation writes a row in `modelmesh_setting_automations`, with the
   prompt encrypted. The app then creates or updates a Temporal Schedule named
   `modelmesh-setting-<id>` with the interval. Overlapping firings are skipped.
2. Each firing starts the workflow `SettingsAutomationWorkflow`, which runs the
   activity `modelmesh_settings_admit`.
3. The activity records a receipt for the occurrence, then submits the prompt
   with the request key `automation:<hash>`. A retried activity finds the
   existing receipt and the existing turn, so one firing never creates two turns.
4. The dispatcher starts the turn like any other
   (see [Architecture](architecture.md#request-flow)).
5. If the owner's sign-in was revoked since the automation was last saved,
   paused or resumed, the run is recorded as denied and no turn starts.

The code is in `native/src/modelmesh/settings_runtime.py` and
`native/src/modelmesh/settings_api.py`. The API is under `/api/settings/automations`.
