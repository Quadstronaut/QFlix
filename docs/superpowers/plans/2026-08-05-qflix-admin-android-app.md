# QFlix Admin — Android App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the shipped QFlix Heartbeat read-only dashboard into QFlix Admin — a three-page app that can also fire remediation actions through the SSH dispatcher.

**Architecture:** The existing Compose app keeps its transport, provisioning and theme. `StatusTransport` becomes verb-aware, a typed `Envelope` model replaces raw-JSON handling, a navigation drawer wraps three screens, and two new screens (Apps, stARR) are added beside the existing Dashboard.

**Tech Stack:** Kotlin, Jetpack Compose (Material 3), sshj + BouncyCastle, kotlinx.serialization, JUnit. Gradle wrapper in `apps/heartbeat-android/`. JDK 17.

**Spec:** `docs/superpowers/specs/2026-08-03-qflix-admin-android-design.md`
**Plan 1 (server, COMPLETE):** `docs/superpowers/plans/2026-08-03-qflix-admin-server-side.md`

## Global Constraints

- **The wire protocol is fixed by Plan 1 and already deployed.** Every verb returns one JSON envelope:
  ```json
  {"ok": true, "verb": "app.restart", "target": "sonarr",
   "verdict": "restart sonarr (ucc: app-sonarr restart)",
   "lines": ["..."], "elapsed_s": 4.2}
  ```
  `status` additionally carries `doc`; `starr` carries `arrs`; `quota` carries `used_gb`/`total_gb`/`percent`. Do not change the server.
- **The 11 verbs are:** `help`, `status`, `app.list`, `app.start`, `app.stop`, `app.restart`, `arr.search_wanted`, `starr`, `unstick`, `logs`, `quota`. There is no session/watch/member verb and the app must never invent one.
- **PRIVACY:** the app displays content presence and infrastructure state only. No member viewing activity. The server already excludes it; the app must not add a screen that implies otherwise.
- **`ok: false` is a normal outcome, not a crash.** Every verb can return it. Render `verdict` and let the user expand `lines`.
- **Existing seams stay:** `StatusTransport` remains the only network interface so tests and previews keep using `FakeTransport` with no device.
- **Package rename is `com.qflix.heartbeat` → `com.qflix.admin`, label "QFlix Admin".** A changed `applicationId` means Android installs a NEW app; the old one is uninstalled separately, not upgraded.
- **Module path stays `apps/heartbeat-android/`.** Renaming the directory churns the whole diff for no benefit; the app's identity lives in `applicationId` and `android:label`.
- **Assertions come from `org.junit.Assert.*`, NOT `kotlin.test.*`.** This module has no
  `kotlin-test` dependency (checked in `libs.versions.toml` and `build.gradle.kts`), so a
  `kotlin.test` import fails at compile time with `Unresolved reference 'test'` — which is the
  WRONG failure for a TDD step-2 check, since it never reaches the code under test. Every
  existing test file in the repo uses JUnit assertions; match them.
- **Run Gradle from PowerShell, not Git Bash.** Git Bash on this machine dies on the wrapper
  with an `xargs` assertion failure. Use `.\gradlew.bat testDebugUnitTest --console=plain`.
- Build/test from `apps/heartbeat-android/`: `./gradlew testDebugUnitTest` and `./gradlew assembleDebug`.

## File Structure

| File | Responsibility |
|---|---|
| `model/Envelope.kt` (new) | Typed envelope + parser, the one place JSON becomes Kotlin |
| `net/StatusTransport.kt` (modify) | `fetch()` → `exec(verb)` |
| `net/SshFetcher.kt` (modify) | exec the requested verb instead of a hardcoded `"status"` |
| `model/AppRow.kt` (new) | One lifecycle app: slug + class |
| `model/StarrRow.kt` (new) | One *arr: peek counts + disk |
| `ui/AdminScaffold.kt` (new) | Drawer + destination routing |
| `ui/AppsScreen.kt` (new) | 24 rows, lifecycle actions |
| `ui/StarrScreen.kt` (new) | 4 rows, search + peek + disk |
| `ui/ActionViewModel.kt` (new) | Fire a verb, hold verdict + lines |
| `ui/Dashboard.kt` (modify) | Unwrap `doc`, add quota tile + reds-first |
| `app/build.gradle.kts`, `AndroidManifest.xml`, `strings.xml` (modify) | Identity |

---

### Task 1: Envelope model and verb-aware transport

**Files:**
- Create: `app/src/main/java/com/qflix/heartbeat/model/Envelope.kt`
- Modify: `app/src/main/java/com/qflix/heartbeat/net/StatusTransport.kt`
- Modify: `app/src/main/java/com/qflix/heartbeat/net/SshFetcher.kt`
- Modify: `app/src/test/java/com/qflix/heartbeat/net/FakeTransport.kt`
- Test: `app/src/test/java/com/qflix/heartbeat/model/EnvelopeTest.kt`

**Interfaces:**
- Produces: `Envelope(ok, verb, target, verdict, lines, elapsedS, raw)`, `Envelope.parse(json: String): Result<Envelope>`, `StatusTransport.exec(verb: String): Result<String>`

- [ ] **Step 1: Write the failing test**

```kotlin
package com.qflix.heartbeat.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class EnvelopeTest {

    @Test
    fun `parses a success envelope`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"app.restart","target":"sonarr",
                "verdict":"restart sonarr","lines":["a","b"],"elapsed_s":4.2}"""
        ).getOrThrow()
        assertTrue(e.ok)
        assertEquals("app.restart", e.verb)
        assertEquals("sonarr", e.target)
        assertEquals(listOf("a", "b"), e.lines)
        assertEquals(4.2, e.elapsedS, 0.001)
    }

    @Test
    fun `a failure envelope is data, not an error`() {
        // ok=false is a normal server answer - the app renders the verdict.
        val e = Envelope.parse(
            """{"ok":false,"verb":"logs","target":"listmonk",
                "verdict":"listmonk logs are not exposed over this verb",
                "lines":[],"elapsed_s":0.01}"""
        ).getOrThrow()
        assertFalse(e.ok)
        assertTrue(e.verdict.contains("not exposed"))
    }

    @Test
    fun `a null target survives`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"quota","target":null,"verdict":"x",
                "lines":[],"elapsed_s":0.0}"""
        ).getOrThrow()
        assertEquals(null, e.target)
    }

    @Test
    fun `garbage is a failure, not a crash`() {
        assertTrue(Envelope.parse("not json at all").isFailure)
        assertTrue(Envelope.parse("").isFailure)
    }

    @Test
    fun `raw keeps the extra keys verbs attach`() {
        // status carries `doc`, starr carries `arrs`, quota carries percent.
        // The typed fields cover the six every verb has; `raw` is how screens
        // reach the rest without Envelope growing a field per verb.
        val e = Envelope.parse(
            """{"ok":true,"verb":"quota","target":null,"verdict":"x","lines":[],
                "elapsed_s":0.0,"used_gb":1800.0,"total_gb":2794.0,"percent":64.4}"""
        ).getOrThrow()
        assertTrue(e.raw.containsKey("percent"))
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/heartbeat-android && ./gradlew testDebugUnitTest --tests '*EnvelopeTest*'`
Expected: FAIL — `Envelope` unresolved.

- [ ] **Step 3: Implement**

```kotlin
package com.qflix.heartbeat.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * The one shape every dispatcher verb returns, success or failure.
 *
 * Six fields are common to all 11 verbs. Three verbs attach extra top-level
 * keys (`status` -> doc, `starr` -> arrs, `quota` -> used_gb/total_gb/percent),
 * which is why [raw] is kept: screens reach those through it rather than
 * Envelope growing a nullable field per verb.
 *
 * `ok == false` is a NORMAL answer - a refused verb, an unreachable app, a
 * privacy allowlist rejection. It is data to render, never an exception.
 */
data class Envelope(
    val ok: Boolean,
    val verb: String,
    val target: String?,
    val verdict: String,
    val lines: List<String>,
    val elapsedS: Double,
    val raw: JsonObject,
) {
    companion object {
        private val json = Json { ignoreUnknownKeys = true; isLenient = true }

        /** Never throws; a malformed body is [Result.failure]. */
        fun parse(body: String): Result<Envelope> = runCatching {
            val o = json.parseToJsonElement(body).jsonObject
            Envelope(
                ok = o["ok"]!!.jsonPrimitive.content.toBooleanStrict(),
                verb = o["verb"]!!.jsonPrimitive.content,
                target = o["target"]?.jsonPrimitive?.contentOrNull,
                verdict = o["verdict"]?.jsonPrimitive?.contentOrNull ?: "",
                lines = o["lines"]?.jsonArray?.map { it.jsonPrimitive.content } ?: emptyList(),
                elapsedS = o["elapsed_s"]?.jsonPrimitive?.double ?: 0.0,
                raw = o,
            )
        }
    }
}
```

Then change the transport seam. `StatusTransport`:

```kotlin
interface StatusTransport {
    /**
     * Runs one dispatcher verb and returns its raw JSON envelope.
     *
     * Never throws - every failure (missing provisioning, unreachable host,
     * auth rejection, timeout) is [Result.failure]. A verb that ran and
     * answered `ok:false` is [Result.success] carrying that envelope: the
     * server refusing is not a transport failure.
     */
    suspend fun exec(verb: String): Result<String>
}
```

In `SshFetcher`, rename `fetch()` to `exec(verb: String)`, thread `verb` into `connectAndFetch(config, verb)`, and replace the hardcoded exec:

```kotlin
                // The forced command routes on this; before the dispatcher
                // landed it was ignored, so this string was decorative.
                val cmd = session.exec(verb)
```

Update `FakeTransport` to take a per-verb map so tests can script several verbs.

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/heartbeat-android && ./gradlew testDebugUnitTest`
Expected: PASS, including the pre-existing StatusDoc/ViewState/Provisioning tests.

- [ ] **Step 5: Commit**

```bash
git add apps/heartbeat-android
git commit -m "feat(app): typed envelope and a verb-aware transport"
```

---

### Task 2: App identity — QFlix Admin

**Files:**
- Modify: `app/build.gradle.kts`, `app/src/main/res/values/strings.xml`

- [ ] **Step 1: Change applicationId and label**

In `app/build.gradle.kts` set `applicationId = "com.qflix.admin"`. Leave `namespace = "com.qflix.heartbeat"` — the Kotlin package is not the app identity, and renaming it churns every file for no user-visible gain. Add a comment saying exactly that.

In `strings.xml` set `app_name` to `QFlix Admin`.

- [ ] **Step 2: Verify**

Run: `cd apps/heartbeat-android && ./gradlew assembleDebug`
Then: `./gradlew :app:dependencies --configuration debugRuntimeClasspath > /dev/null` (sanity)
Confirm the APK manifest carries the new id:
`unzip -p app/build/outputs/apk/debug/app-debug.apk AndroidManifest.xml | strings | grep -i qflix | head`

- [ ] **Step 3: Commit**

```bash
git add apps/heartbeat-android
git commit -m "feat(app): rename to QFlix Admin (new applicationId installs as a new app)"
```

---

### Task 3: Navigation drawer

**Files:**
- Create: `ui/AdminScaffold.kt`
- Modify: `MainActivity.kt`
- Test: `app/src/test/java/com/qflix/heartbeat/ui/DestinationTest.kt`

**Interfaces:**
- Produces: `enum class Destination(val label: String) { DASHBOARD, APPS, STARR }`, `@Composable fun AdminScaffold(...)`

- [ ] **Step 1: Write the failing test**

```kotlin
package com.qflix.heartbeat.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DestinationTest {
    @Test
    fun `dashboard is first so existing muscle memory survives`() {
        assertEquals(Destination.DASHBOARD, Destination.values().first())
    }

    @Test
    fun `three destinations, labelled for the drawer`() {
        assertEquals(listOf("Dashboard", "Apps", "stARR"),
            Destination.values().map { it.label })
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/heartbeat-android && ./gradlew testDebugUnitTest --tests '*DestinationTest*'`
Expected: FAIL — `Destination` unresolved.

- [ ] **Step 3: Implement**

`Destination` enum plus an `AdminScaffold` using Material 3 `ModalNavigationDrawer` + `Scaffold` with a `TopAppBar` whose navigation icon opens the drawer. Hold the selected destination in `rememberSaveable` so it survives rotation. Route to `DashboardScreen`, `AppsScreen`, `StarrScreen`. `MainActivity` calls `AdminScaffold` instead of `DashboardScreen` directly.

- [ ] **Step 4: Run to verify it passes; then build**

Run: `./gradlew testDebugUnitTest && ./gradlew assembleDebug`

- [ ] **Step 5: Commit**

```bash
git add apps/heartbeat-android
git commit -m "feat(app): hideable drawer over three destinations"
```

---

### Task 4: Action plumbing — fire a verb, show verdict + lines

**Files:**
- Create: `ui/ActionViewModel.kt`
- Create: `ui/VerdictSheet.kt`
- Test: `app/src/test/java/com/qflix/heartbeat/ui/ActionViewModelTest.kt`

**Interfaces:**
- Produces: `sealed class ActionState { Idle; Running(verb); Done(Envelope); Failed(message) }`, `ActionViewModel.fire(verb: String)`

- [ ] **Step 1: Write the failing test**

```kotlin
package com.qflix.heartbeat.ui

import com.qflix.heartbeat.net.FakeTransport
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ActionViewModelTest {

    @Test
    fun `a refused verb is Done with ok=false, not Failed`() = runTest {
        // The server refusing is an ANSWER. Failed is reserved for the
        // transport never getting one.
        val t = FakeTransport(mapOf("logs listmonk" to
            """{"ok":false,"verb":"logs","target":"listmonk",
                "verdict":"listmonk logs are not exposed over this verb",
                "lines":[],"elapsed_s":0.0}"""))
        val vm = ActionViewModel(t)
        vm.fire("logs listmonk")
        val s = vm.state.value
        assertTrue(s is ActionState.Done)
        assertEquals(false, (s as ActionState.Done).envelope.ok)
    }

    @Test
    fun `a transport failure is Failed`() = runTest {
        val t = FakeTransport(emptyMap())      // returns Result.failure
        val vm = ActionViewModel(t)
        vm.fire("status")
        assertTrue(vm.state.value is ActionState.Failed)
    }

    @Test
    fun `unparseable body is Failed, not a crash`() = runTest {
        val t = FakeTransport(mapOf("status" to "<html>gateway timeout</html>"))
        val vm = ActionViewModel(t)
        vm.fire("status")
        assertTrue(vm.state.value is ActionState.Failed)
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `./gradlew testDebugUnitTest --tests '*ActionViewModelTest*'`
Expected: FAIL — `ActionViewModel` unresolved.

- [ ] **Step 3: Implement**

`ActionViewModel(transport)` exposing `StateFlow<ActionState>`; `fire(verb)` sets `Running`, calls `transport.exec(verb)`, parses with `Envelope.parse`, and lands on `Done(envelope)` or `Failed(message)`. `VerdictSheet` is a Compose bottom sheet showing `verdict` prominently with an expandable `lines` block — collapsed by default, per "verdict + last lines on demand".

- [ ] **Step 4: Run to verify it passes**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(app): action plumbing - refusal is an answer, not a failure"
```

---

### Task 5: Apps screen

**Files:**
- Create: `model/AppRow.kt`, `ui/AppsScreen.kt`
- Test: `app/src/test/java/com/qflix/heartbeat/model/AppRowTest.kt`

**Interfaces:**
- Produces: `AppRow(slug, klass)`, `AppRow.parseList(envelope: Envelope): List<AppRow>`

- [ ] **Step 1: Write the failing test**

```kotlin
package com.qflix.heartbeat.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AppRowTest {
    @Test
    fun `parses the app_list lines into slug and class`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"app.list","target":null,
                "verdict":"24 apps with a lifecycle",
                "lines":["sonarr ucc","listmonk systemd"],"elapsed_s":0.1}"""
        ).getOrThrow()
        assertEquals(listOf(AppRow("sonarr", "ucc"), AppRow("listmonk", "systemd")),
                     AppRow.parseList(e))
    }

    @Test
    fun `a malformed line is skipped, not crashed on`() {
        val e = Envelope.parse(
            """{"ok":true,"verb":"app.list","target":null,"verdict":"x",
                "lines":["sonarr ucc","garbage"],"elapsed_s":0.1}"""
        ).getOrThrow()
        assertEquals(listOf(AppRow("sonarr", "ucc")), AppRow.parseList(e))
    }
}
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

`AppRow.parseList` splits each line on whitespace, keeping only 2-token lines. `AppsScreen` lists the rows with the class as a visible badge — the spec requires the UCC/systemd distinction be *visible*, so the operator can see an Ultra-managed app is driven by approved commands. Each row has start / stop / restart, which call `ActionViewModel.fire("app.<action> <slug>")` and surface the result in `VerdictSheet`.

- [ ] **Step 4: Run to verify it passes; build**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(app): Apps screen - 24 rows with a visible class badge"
```

---

### Task 6: stARR screen

**Files:**
- Create: `model/StarrRow.kt`, `ui/StarrScreen.kt`
- Test: `app/src/test/java/com/qflix/heartbeat/model/StarrRowTest.kt`

**Interfaces:**
- Produces: `StarrRow(slug, kind, titleCount, completeCount, human, ok, error)`, `StarrRow.parseAll(envelope: Envelope): List<StarrRow>`

- [ ] **Step 1: Write the failing test**

```kotlin
package com.qflix.heartbeat.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StarrRowTest {

    private val body = """
      {"ok":true,"verb":"starr","target":null,"verdict":"4 *arrs",
       "lines":[],"elapsed_s":0.7,
       "arrs":{
         "sonarr":{"peek":{"slug":"sonarr","kind":"series","ok":true,"error":"",
                           "titles":[{"title":"A","have":12,"total":30,"complete":false},
                                     {"title":"B","have":10,"total":10,"complete":true}]},
                   "usage":{"slug":"sonarr","bytes":1073741824,"human":"1.0 GB",
                            "title_count":2,"ok":true,"error":""}},
         "radarr2":{"peek":{"slug":"radarr2","kind":"movie","ok":false,
                            "error":"refused","titles":[]},
                    "usage":{"slug":"radarr2","bytes":0,"human":"0.0 B",
                             "title_count":0,"ok":false,"error":"refused"}}}}
    """.trimIndent()

    @Test
    fun `counts titles and completes per arr`() {
        val rows = StarrRow.parseAll(Envelope.parse(body).getOrThrow())
        val s = rows.first { it.slug == "sonarr" }
        assertEquals(2, s.titleCount)
        assertEquals(1, s.completeCount)
        assertEquals("1.0 GB", s.human)
        assertTrue(s.ok)
    }

    @Test
    fun `a degraded arr is a row, not a missing row`() {
        // One dead *arr must not blank the page - the server keeps ok=true
        // overall and marks the instance. The UI must show it as degraded.
        val rows = StarrRow.parseAll(Envelope.parse(body).getOrThrow())
        val r = rows.first { it.slug == "radarr2" }
        assertFalse(r.ok)
        assertEquals("refused", r.error)
    }

    @Test
    fun `no consumption data is read out of the payload`() {
        // Structural: StarrRow has no field that could hold a member identity.
        val fields = StarrRow::class.java.declaredFields.map { it.name.lowercase() }
        val banned = listOf("watch", "view", "user", "session", "played", "seen")
        assertTrue(fields.none { f -> banned.any { f.contains(it) } }, fields.toString())
    }
}
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

`StarrRow.parseAll` walks `envelope.raw["arrs"]`, reading `peek.titles` (count + how many `complete`) and `usage.human` / `usage.ok`. `StarrScreen` renders four rows: slug, `12/30 complete`-style summary, disk figure, a **Search all wanted** button firing `arr.search_wanted <slug>`, and a plain **Open in browser** button. A row whose `ok` is false renders degraded with its `error`, not omitted. The whole page is painted from ONE `starr` call — do not fire per-row verbs on load.

- [ ] **Step 4: Run to verify it passes; build**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(app): stARR screen from one round trip"
```

---

### Task 7: Dashboard rework, build, install

**Files:**
- Modify: `ui/Dashboard.kt`, `ui/StatusViewModel.kt`

- [ ] **Step 1: Unwrap the envelope**

`StatusViewModel` now calls `transport.exec("status")`, parses an `Envelope`, and reads the dashboard document from `envelope.raw["doc"]` instead of treating the whole body as the doc. Existing `StatusDoc` parsing is unchanged below that point. Update `FakeTransport` fixtures accordingly and keep every existing StatusDoc/ViewState test passing.

- [ ] **Step 2: Add the quota tile**

Fire `quota` alongside `status` and render used/total GB plus percent. Source it from the `quota` verb, not from the status doc's own quota section, so the tile has one owner.

- [ ] **Step 3: Reds first**

Sort the Kuma section so monitors currently DOWN pin to the top. Do not add a "fix it" button in this task — routing a red to its remediating action is a follow-up once the Apps screen is proven.

- [ ] **Step 4: Full test + build**

```bash
cd apps/heartbeat-android
./gradlew testDebugUnitTest
./gradlew assembleDebug
```
Expected: all unit tests pass; APK at `app/build/outputs/apk/debug/app-debug.apk`.

- [ ] **Step 5: Install to the connected phone**

```bash
adb devices                       # confirm exactly one device
adb install -r app/build/outputs/apk/debug/app-debug.apk
```
`-r` reinstalls in place. Because `applicationId` changed, this installs as a NEW app beside the old Heartbeat one rather than replacing it — that is intended, and the old app keeps working against its own key.

- [ ] **Step 6: Provision the key bundle onto the device**

The app reads its key bundle from app-private `filesDir`. Push the three files minted in Plan 1 Task 9:

```bash
adb push .admin-key/qflix-admin      /sdcard/Android/data/com.qflix.admin/files/qflix-admin
adb push .admin-key/known_host       /sdcard/Android/data/com.qflix.admin/files/known_host
```
If app-private `filesDir` is not reachable over adb without root, add a one-time in-app import screen instead and say so — do NOT weaken the key's storage location to make adb push work.

- [ ] **Step 7: Commit**

```bash
git commit -am "feat(app): dashboard on the envelope, quota tile, reds first"
```

---

## Self-Review

**Spec coverage.** Drawer + three destinations → Task 3. Apps page with class badge and lifecycle actions → Task 5. stARR with search / peek / disk / browser button, one round trip → Task 6. Dashboard reds-first + quota tile → Task 7. Identity rename → Task 2. Verdict + expandable lines → Task 4. Privacy → enforced server-side and re-asserted structurally in Task 6's field test.

**Deliberately not built:** autologin to *arr web UIs (spec: out of scope), push notifications (Kuma already pages via Discord), any member-activity view, and red→action deep-linking (Task 7 Step 3 defers it explicitly rather than half-building it).

**Type consistency.** `Envelope` (Task 1) is consumed unchanged by `ActionViewModel` (4), `AppRow` (5), `StarrRow` (6) and `StatusViewModel` (7). `StatusTransport.exec(verb)` is the single seam all four use. `FakeTransport` takes a verb→body map from Task 1 onward, so later tasks add fixtures rather than changing its shape.

**Known risk.** Task 7 Step 6 assumes the key bundle can be placed in app-private storage over adb. On modern Android that path may be inaccessible without root; the step names the fallback (in-app import) rather than pretending it will work.
