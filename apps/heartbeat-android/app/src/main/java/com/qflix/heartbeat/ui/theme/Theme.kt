package com.qflix.heartbeat.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

private val HeartbeatColorScheme = darkColorScheme(
    primary = QflixAccent,
    background = QflixBackground,
    surface = QflixSurface,
    surfaceVariant = QflixSurfaceVariant,
    onBackground = QflixOnBackground,
    onSurface = QflixOnBackground,
)

/**
 * QFlix Heartbeat is a status dashboard meant to be glanced at in low light —
 * it is intentionally dark-only and does not follow the system light/dark
 * setting (unlike most Compose starter themes).
 */
@Composable
fun HeartbeatTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = HeartbeatColorScheme,
        typography = Typography,
        content = content,
    )
}
