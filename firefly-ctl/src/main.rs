// firefly-ctl: standalone controller for the Holtek "Firefly" 4-zone/per-key
// RGB keyboard (USB 04d9:a1cd). Replaces the Firefly-cli prototype as the
// backend used by hypr-util, with the brightness/intensity byte exposed as
// a real argument instead of a hardcoded constant.
use clap::Parser;
use std::time::Duration;

use rusb::GlobalContext;

const VENDOR_ID: u16 = 0x04d9;
const PRODUCT_ID: u16 = 0xa1cd;
const INTERFACE: u8 = 2;
const COLOR_COUNT: usize = 7;

fn get_device(vendor_id: u16, product_id: u16) -> rusb::Device<GlobalContext> {
    for device in rusb::devices().unwrap().iter() {
        let desc = device.device_descriptor().unwrap();
        if desc.vendor_id() == vendor_id && desc.product_id() == product_id {
            return device;
        }
    }
    panic!("device not found");
}

#[allow(non_camel_case_types)]
#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum Effect {
    STATIC = 0,
    BREATHE = 1,
    FADE = 2,
    GETTING_OFF = 3,
    LITTLE_STARS = 4,
    LASER = 5,
    WAVE = 6,
    NEON = 7,
    RAINDROP = 8,
    RIPPLE = 9,
    WAVE2 = 10,
    SWIRL = 11,
}

struct Firefly {
    handle: rusb::DeviceHandle<GlobalContext>,
}

impl Firefly {
    fn new() -> Self {
        let device = get_device(VENDOR_ID, PRODUCT_ID);
        let handle = device.open().expect("failed to open device");
        handle
            .set_auto_detach_kernel_driver(true)
            .expect("failed to enable auto-detach of kernel driver");
        handle
            .claim_interface(INTERFACE)
            .expect("failed to claim interface 2");
        Firefly { handle }
    }

    fn header_request(&self) {
        let data = [0x30u8, 0x00, 0x00, 0x00, 0x00, 0x55, 0xaa, 0x00];
        self.handle
            .write_control(0x21, 0x09, 0x0300, INTERFACE as u16, &data, Duration::from_secs(1))
            .expect("header request failed");
    }

    fn color_request(&self, colors: &[[u8; 3]]) {
        let mut buf: Vec<u8> = colors.iter().flat_map(|c| c.iter().copied()).collect();
        buf.resize(64, 0);
        self.handle
            .write_interrupt(0x04, &buf, Duration::from_secs(1))
            .expect("color request failed");
    }

    fn effects_request(&self, effect: Effect, color_idx: u8, brightness: u8) {
        assert!(color_idx <= 7, "color index out of bounds: {}", color_idx);
        let data = [
            0x08u8,
            effect as u8,
            0x3f,
            brightness,
            0x00,
            color_idx,
            0xc4,
            0x3b,
        ];
        self.handle
            .write_control(0x21, 0x09, 0x0300, INTERFACE as u16, &data, Duration::from_secs(1))
            .expect("effects request failed");
    }

    fn release(&self) {
        let _ = self.handle.release_interface(INTERFACE);
    }
}

fn decode_hex_color(s: &str) -> [u8; 3] {
    let s = s.strip_prefix('#').unwrap_or(s);
    assert_eq!(s.len(), 6, "invalid color {:?}, expected 6 hex digits", s);
    let bytes: Vec<u8> = (0..6)
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("invalid hex digit"))
        .collect();
    // Device expects 6-bit (0-63) per channel.
    [bytes[0] / 4, bytes[1] / 4, bytes[2] / 4]
}

#[derive(Parser, Debug)]
#[command(version, about = "Controller for the Holtek Firefly RGB keyboard")]
struct Args {
    #[arg(short, long, value_enum)]
    effect: Effect,

    #[arg(
        short,
        long,
        help = "Exactly 7 colors, comma-delimited hex (e.g. ff0000,00ff00,...). Defaults to a stock rainbow.",
        num_args = 0..,
        value_delimiter = ','
    )]
    colors: Vec<String>,

    #[arg(
        long = "ci",
        help = "0-6 selects a single color slot; 7 cycles through all 7",
        default_value = "7"
    )]
    color_idx: u8,

    #[arg(
        short,
        long,
        help = "Intensity/brightness byte sent with the effect command (0-255). 0x05 looked correct in testing; the device's own default of 0x01 renders washed out.",
        default_value = "5"
    )]
    brightness: u8,
}

fn main() {
    let args = Args::parse();

    let default_colors = [
        "ff0000", "00ff00", "ffff00", "0000ff", "00ffff", "ff00ff", "ffffff",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect::<Vec<_>>();

    let color_strings = if args.colors.is_empty() {
        default_colors
    } else {
        assert_eq!(args.colors.len(), COLOR_COUNT, "exactly 7 colors are required");
        args.colors
    };
    let colors: Vec<[u8; 3]> = color_strings.iter().map(|c| decode_hex_color(c)).collect();

    assert!(args.color_idx <= 7, "color index out of bounds");

    let fx = Firefly::new();
    fx.header_request();
    fx.color_request(&colors);
    fx.effects_request(args.effect, args.color_idx, args.brightness);
    fx.release();
}
