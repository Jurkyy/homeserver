//! homeserver patch (not upstream): a tiny local control socket for the
//! running Spirc, so the homeserver portal / Android app can play, pause,
//! skip, seek and set the volume of the Spotify Connect session.
//!
//! librespot has no control API of its own (no MPRIS/D-Bus, no HTTP); the
//! `Spirc` handle in main.rs is the only way in. The Dockerfile copies this
//! file to src/control.rs and applies control.patch, which wires it into
//! main's select loop.
//!
//! Enabled only when both env vars are set:
//!   LIBRESPOT_CONTROL_ADDR   e.g. 0.0.0.0:36422 (host network)
//!   LIBRESPOT_CONTROL_TOKEN  shared secret (mediacast's MEDIACAST_TOKEN)
//!
//! Protocol: one request per TCP connection, a single line
//!   `<token> <command> [arg]\n`
//! answered with `ok\n` or `err <reason>\n`. Commands:
//!   play | pause | playpause | next | prev | seek <ms> | volume <0-65535>
//! `play` first asks Spotify to transfer playback here, so it also resumes
//! a session that went inactive (the transfer is ignored when already the
//! active device).

use librespot::connect::Spirc;
use log::{error, info, warn};
use std::time::Duration;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    sync::{mpsc, oneshot},
    time::timeout,
};

#[derive(Debug)]
pub enum Command {
    Play,
    Pause,
    PlayPause,
    Next,
    Prev,
    Seek(u32),
    Volume(u16),
}

pub type Request = (Command, oneshot::Sender<Result<(), String>>);

const IO_TIMEOUT: Duration = Duration::from_secs(5);

/// Starts the listener if configured; requests arrive on the returned
/// channel and must be answered from the task that owns the Spirc.
pub fn spawn() -> Option<mpsc::UnboundedReceiver<Request>> {
    let addr = std::env::var("LIBRESPOT_CONTROL_ADDR").ok().filter(|s| !s.is_empty())?;
    let Some(token) = std::env::var("LIBRESPOT_CONTROL_TOKEN")
        .ok()
        .filter(|s| !s.is_empty())
    else {
        warn!("control: LIBRESPOT_CONTROL_ADDR set but LIBRESPOT_CONTROL_TOKEN is empty; disabled");
        return None;
    };
    let (tx, rx) = mpsc::unbounded_channel();
    tokio::spawn(async move {
        let listener = match TcpListener::bind(&addr).await {
            Ok(l) => l,
            Err(e) => {
                error!("control: cannot listen on {addr}: {e}");
                return;
            }
        };
        info!("control: listening on {addr}");
        loop {
            match listener.accept().await {
                Ok((sock, _)) => {
                    let tx = tx.clone();
                    let token = token.clone();
                    tokio::spawn(async move {
                        if let Err(e) = handle(sock, tx, &token).await {
                            warn!("control: {e}");
                        }
                    });
                }
                Err(e) => {
                    warn!("control: accept failed: {e}");
                    tokio::time::sleep(Duration::from_millis(200)).await;
                }
            }
        }
    });
    Some(rx)
}

fn parse(cmd: &str, arg: Option<&str>) -> Result<Command, String> {
    let num = |what: &str| -> Result<u64, String> {
        arg.and_then(|a| a.parse::<u64>().ok())
            .ok_or_else(|| format!("{what} needs a numeric argument"))
    };
    Ok(match cmd {
        "play" => Command::Play,
        "pause" => Command::Pause,
        "playpause" => Command::PlayPause,
        "next" => Command::Next,
        "prev" | "previous" => Command::Prev,
        "seek" => Command::Seek(num("seek")?.min(u32::MAX as u64) as u32),
        "volume" => Command::Volume(num("volume")?.min(u16::MAX as u64) as u16),
        other => return Err(format!("unknown command {other:?}")),
    })
}

fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

async fn handle(
    sock: TcpStream,
    tx: mpsc::UnboundedSender<Request>,
    token: &str,
) -> Result<(), String> {
    let (rd, mut wr) = sock.into_split();
    let mut line = String::new();
    let mut rd = BufReader::new(rd.take(4096));
    timeout(IO_TIMEOUT, rd.read_line(&mut line))
        .await
        .map_err(|_| "read timeout".to_string())?
        .map_err(|e| e.to_string())?;
    let mut parts = line.split_whitespace();
    let reply = if !same(parts.next().unwrap_or("").as_bytes(), token.as_bytes()) {
        Err("unauthorized".to_string())
    } else {
        match parse(parts.next().unwrap_or(""), parts.next()) {
            Err(e) => Err(e),
            Ok(cmd) => {
                let (otx, orx) = oneshot::channel();
                if tx.send((cmd, otx)).is_err() {
                    Err("librespot is shutting down".to_string())
                } else {
                    match timeout(IO_TIMEOUT, orx).await {
                        Ok(Ok(r)) => r,
                        _ => Err("no answer from main loop".to_string()),
                    }
                }
            }
        }
    };
    let out = match reply {
        Ok(()) => "ok\n".to_string(),
        Err(e) => format!("err {e}\n"),
    };
    timeout(IO_TIMEOUT, wr.write_all(out.as_bytes()))
        .await
        .map_err(|_| "write timeout".to_string())?
        .map_err(|e| e.to_string())
}

/// Runs a command against the live Spirc (called from main's select loop).
/// Spirc silently ignores most commands while this isn't the active
/// Connect device; that's not an error here.
pub fn apply(spirc: Option<&Spirc>, cmd: Command) -> Result<(), String> {
    let Some(spirc) = spirc else {
        return Err("no Spotify session (pick this device in a Spotify app once)".to_string());
    };
    let r = match cmd {
        Command::Play => spirc.transfer(None).and_then(|_| spirc.play()),
        Command::Pause => spirc.pause(),
        Command::PlayPause => spirc.play_pause(),
        Command::Next => spirc.next(),
        Command::Prev => spirc.prev(),
        Command::Seek(ms) => spirc.set_position_ms(ms),
        Command::Volume(v) => spirc.set_volume(v),
    };
    r.map_err(|e| e.to_string())
}
