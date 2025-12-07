# AT Simulator

A lightweight, PTY-based AT command simulator written in Python.  
It emulates a virtual modem that responds to AT commands, supports artificial delays, and can simulate full modem reboot behaviour.  
Useful for developing modem-aware applications, automated testing, CI pipelines, and debugging AT command parsers without real hardware.

## Capabilities

### ✔ Cross-platform: Linux + macOS  
Works on both OSes using their native PTY systems.  
Details on differences are listed below.

### ✔ Real PTY modem interface  
Creates a real terminal device:
- Linux: `/dev/pts/X`
- macOS: `/dev/ttysXXX`

Any client program can open it:
```
screen /dev/pts/7 115200
screen /tmp/fake_modem 115200
```

### ✔ Configurable AT responses  
All responses are defined in `commands.json`.  
Supports:
- Multi-line responses
- `{arg}` substitution
- Internal placeholders
- Per-command artificial delays

### ✔ Simulated modem reboot  
`AT+CFUN=1,1` performs a true reboot simulation:
- Current PTY is closed  
- New PTY is allocated  
- Symlink updated  
- Boot banner displayed  

### ✔ Delayed responses  
`AT+DELAY=n` injects an `n` millisecond delay before replying.

### ✔ Clean, structured logging  
Verbose mode prints aligned, colorized logs:
```
[12:21:15.102]  [← RX ]  AT+CSQ
[12:21:15.203]  [→ TX ]  +CSQ: 20,99 | OK
```

## Cross-Platform Behaviour  

### Linux
- Uses `/dev/pts/*` PTY devices.
- Typically no permission issues.
- Symlinks behave predictably and update during reboot.
- Compatible with screen, minicom, modemmanager, etc.

### macOS
- Uses `/dev/ttysXXX` BSD-style terminal devices.
- May require permission adjustments:
```
sudo chmod 666 /dev/ttys005
```
- Symlinks update correctly after reboot.
- Compatible with screen, CoolTerm, Serial Tools, etc.

## Internal Architecture

### PTY Creation  
`pty.openpty()` allocates a master/slave PTY pair.  
The *slave* acts as the modem; the *master* is controlled by the simulator.

### Command Flow  
1. Client writes `AT+CMD`.  
2. Simulator parses and evaluates.  
3. Optional delay is applied.  
4. Response is written back over the PTY.  

### Reboot Flow  
Triggered by `AT+CFUN=1,1`:
- Close old PTY  
- Allocate new PTY  
- Update symlink  
- Restart modem banner  

## Installation
```
git clone https://github.com/yourname/atc.git
cd atc
chmod +x fake_atc.py
```

## Usage

Run normally:
```
./fake_atc.py
```

Create stable symlink:
```
./fake_atc.py --target /tmp/fake_modem
```

Enable verbose logs:
```
./fake_atc.py -v
```

Connect:
```
screen /tmp/fake_modem 115200
```

## Example commands.json

```json
{
  "AT": "OK",
  "AT+GMI": "GenericManufacturer\r\nOK",
  "AT+CSQ": { "delay": 200, "resp": "+CSQ: 20,99\r\nOK" },
  "AT+PING={arg}": "PONG {arg}\r\nOK"
}
```

## Special Commands

### AT+DELAY=n
Injects artificial delay. ('n' in milliseconds)

### AT+CFUN=1,1
Triggers simulated modem reboot.
