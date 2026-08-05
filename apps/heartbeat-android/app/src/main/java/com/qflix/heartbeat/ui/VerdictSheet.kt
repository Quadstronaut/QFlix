package com.qflix.heartbeat.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.qflix.heartbeat.model.Envelope

/**
 * Bottom sheet showing the outcome of one fired verb - [ActionState.Running]'s
 * verb while in flight, then whichever of [ActionState.Done] /
 * [ActionState.Failed] it lands on.
 *
 * The `verdict` string is the headline regardless of `ok`: a refusal
 * ("unstick refused: sonarr is red") IS the useful information, not a
 * generic error banner to swallow. `lines` is supporting detail only -
 * collapsed by default, expandable on tap, per "verdict + last lines on
 * demand".
 *
 * Renders nothing for [ActionState.Idle] - there is no sheet to show before
 * anything has been fired.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VerdictSheet(state: ActionState, onDismiss: () -> Unit) {
    if (state is ActionState.Idle) return

    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp)
                .padding(bottom = 24.dp),
        ) {
            when (state) {
                is ActionState.Running -> RunningBody(state.verb)
                is ActionState.Done -> DoneBody(state.envelope)
                is ActionState.Failed -> FailedBody(state.message)
                ActionState.Idle -> Unit
            }
        }
    }
}

@Composable
private fun RunningBody(verb: String) {
    Text(verb, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
    Spacer(Modifier.height(12.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
        CircularProgressIndicator(modifier = Modifier.size(20.dp))
        Spacer(Modifier.width(12.dp))
        Text("Running…", style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun DoneBody(envelope: Envelope) {
    // The verdict is the headline whether ok is true or false - a refusal
    // is the whole point of the sheet, not something to hide behind a
    // generic "failed" label.
    Text(
        text = envelope.verdict,
        style = MaterialTheme.typography.titleMedium,
        fontWeight = FontWeight.Bold,
        color = if (envelope.ok) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.error,
    )
    if (envelope.lines.isNotEmpty()) {
        var expanded by remember { mutableStateOf(false) }
        Spacer(Modifier.height(8.dp))
        TextButton(onClick = { expanded = !expanded }) {
            Text(if (expanded) "Hide ${envelope.lines.size} line(s)" else "Show ${envelope.lines.size} line(s)")
        }
        if (expanded) {
            Column {
                envelope.lines.forEach { line ->
                    Text(
                        text = line,
                        style = MaterialTheme.typography.bodySmall,
                        fontFamily = FontFamily.Monospace,
                    )
                }
            }
        }
    }
}

@Composable
private fun FailedBody(message: String) {
    Text(
        "Failed",
        style = MaterialTheme.typography.titleMedium,
        fontWeight = FontWeight.Bold,
        color = MaterialTheme.colorScheme.error,
    )
    Spacer(Modifier.height(8.dp))
    Text(message, style = MaterialTheme.typography.bodyMedium)
}
