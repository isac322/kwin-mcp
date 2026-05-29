import QtQuick
import QtQuick.Controls

ApplicationWindow {
    width: 1920; height: 1080
    visibility: ApplicationWindow.FullScreen
    visible: true
    title: "a11y smoke"
    Column {
        anchors.centerIn: parent
        spacing: 12
        TextField {
            id: entry
            width: 220
            placeholderText: "Type here"
            Accessible.id: "entry-field"
            Accessible.name: "Smoke entry"
        }
        Button {
            id: ping
            text: "Ping"
            Accessible.id: "ping-button"
            Accessible.name: "Ping button"
            onClicked: status.text = entry.text || "clicked"
        }
        Label {
            id: status
            text: "ready"
            Accessible.id: "status-text"
            Accessible.name: "Status text: " + text
        }
    }
}
