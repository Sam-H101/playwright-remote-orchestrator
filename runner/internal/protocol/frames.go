package protocol

import (
	"encoding/json"
	"errors"
)

// Slot is one entry of register.slots / heartbeat.slots.
type Slot struct {
	Index   int  `json:"index"`
	Healthy bool `json:"healthy"`
}

// Register is the first frame on the control channel. Field order matches agent.js.
type Register struct {
	Type              string   `json:"type"`
	Name              string   `json:"name"`
	Slots             []Slot   `json:"slots"`
	PlaywrightVersion string   `json:"playwrightVersion"`
	AgentVersion      string   `json:"agentVersion"`
	Platform          string   `json:"platform"`
	Browsers          []string `json:"browsers"`
	Ephemeral         bool     `json:"ephemeral"`
}

// Heartbeat reports slot health; the hub applies it only once the node is registered.
type Heartbeat struct {
	Type  string `json:"type"`
	Slots []Slot `json:"slots"`
}

// OpenFailed tells the hub an open could not be served. Status is run-server's HTTP status when it
// refused the upgrade (428 means the client's Playwright version).
type OpenFailed struct {
	Type    string `json:"type"`
	ID      string `json:"id"`
	Message string `json:"message"`
	Status  *int   `json:"status,omitempty"`
}

// MaxOpenFailedMessage is how much of an open-failed message the hub keeps.
const MaxOpenFailedMessage = 4000

// Open asks the agent to connect a client to a slot. Every field but the id is kept raw and validated
// by the session code, which must never fail on a bad frame.
type Open struct {
	ID      string
	Slot    json.RawMessage
	Path    json.RawMessage
	Query   json.RawMessage
	Headers map[string]json.RawMessage
}

// Inbound is a parsed control frame: its type and the whole object.
type Inbound struct {
	Type string
	Raw  json.RawMessage
}

var errMalformed = errors.New("expected a JSON control frame")

// ParseInbound accepts a JSON object with a string "type"; anything else is malformed.
func ParseInbound(data []byte) (Inbound, error) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(data, &obj); err != nil || obj == nil {
		return Inbound{}, errMalformed
	}
	var typ string
	if err := json.Unmarshal(obj["type"], &typ); err != nil {
		return Inbound{}, errMalformed
	}
	return Inbound{Type: typ, Raw: data}, nil
}

// ParseOpen extracts an open frame's fields; an id that is not a string comes back as "".
func ParseOpen(raw json.RawMessage) Open {
	var obj map[string]json.RawMessage
	_ = json.Unmarshal(raw, &obj)
	var o Open
	_ = json.Unmarshal(obj["id"], &o.ID)
	o.Slot, o.Path, o.Query = obj["slot"], obj["path"], obj["query"]
	_ = json.Unmarshal(obj["headers"], &o.Headers)
	return o
}

// StringField returns a string field of a frame ("" when absent or not a string) and whether it was one.
func StringField(raw json.RawMessage, name string) (string, bool) {
	var obj map[string]json.RawMessage
	if json.Unmarshal(raw, &obj) != nil {
		return "", false
	}
	var s string
	if json.Unmarshal(obj[name], &s) != nil {
		return "", false
	}
	return s, true
}
