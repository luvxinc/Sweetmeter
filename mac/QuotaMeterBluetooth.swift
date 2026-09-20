import Foundation
import CoreBluetooth
import Darwin

let serviceID = CBUUID(string: "7a1e0001-ff1b-4d9f-a023-47c7752c1a01")
let controlID = CBUUID(string: "7a1e0002-ff1b-4d9f-a023-47c7752c1a01")
let dataID = CBUUID(string: "7a1e0003-ff1b-4d9f-a023-47c7752c1a01")
let statusID = CBUUID(string: "7a1e0004-ff1b-4d9f-a023-47c7752c1a01")
func emit(_ object: [String: Any]) {
    if let bytes = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(bytes + Data([10]))
    }
}
func little32(_ value: UInt32) -> [UInt8] { (0..<4).map { UInt8(truncatingIfNeeded: value >> ($0 * 8)) } }
func read32(_ bytes: [UInt8], _ offset: Int) -> UInt32 {
    (0..<4).reduce(0) { $0 | (UInt32(bytes[offset + $1]) << ($1 * 8)) }
}
func crc32(_ bytes: Data) -> UInt32 {
    var crc: UInt32 = 0xffffffff
    for byte in bytes {
        crc ^= UInt32(byte)
        for _ in 0..<8 { crc = (crc >> 1) ^ (crc & 1 == 1 ? 0xedb88320 : 0) }
    }
    return ~crc
}

final class Bridge: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate, CBPeripheralManagerDelegate {
    var central: CBCentralManager!
    var beacon: CBPeripheralManager!
    var peripheral: CBPeripheral?
    var control: CBCharacteristic?, payload: CBCharacteristic?, status: CBCharacteristic?
    let hostID: String, hostName: String
    let pinnedID: UUID?
    var frame: Data?, queuedFrame: Data?
    var checksum: UInt32 = 0, sequence: UInt32 = 0
    var offset = 0
    var writes: [String] = []
    var phase = "offline"
    var ready = false
    var lastAction = Date(), retryAt = Date(), statusAt = Date.distantPast
    var timer: Timer?

    init(hostID: String, hostName: String, pinnedID: UUID?) {
        self.hostID = hostID; self.hostName = hostName; self.pinnedID = pinnedID
        super.init()
        central = CBCentralManager(delegate: self, queue: .main)
        beacon = CBPeripheralManager(delegate: self, queue: .main)
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
    }
    func error(_ message: String, retry: Double = 5) {
        emit(["event": "error", "error": message])
        ready = false; retryAt = Date().addingTimeInterval(retry)
        if let peripheral { central.cancelPeripheralConnection(peripheral) }
        else { phase = "offline" }
    }
    func scan() {
        guard central.state == .poweredOn, peripheral == nil, Date() >= retryAt else { return }
        phase = "scan"; central.scanForPeripherals(withServices: [serviceID])
    }
    func tick() {
        if peripheral == nil { if !central.isScanning { scan() }; return }
        if phase != "idle" && Date().timeIntervalSince(lastAction) > 45 {
            error("BLE transfer or connection timed out"); return
        }
        if ready && phase == "idle" {
            if Date().timeIntervalSince(statusAt) > 30, let peripheral, let status {
                phase = "status"; lastAction = Date(); peripheral.readValue(for: status)
            } else { sendPending() }
        }
    }
    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        if peripheral.state == .poweredOn {
            peripheral.startAdvertising([CBAdvertisementDataLocalNameKey: hostName,
                CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: hostID)]])
        }
    }
    func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
        if let error { emit(["event": "error", "error": "Computer discovery: \(error.localizedDescription)"]) }
        else { emit(["event": "advertising", "host_id": hostID, "name": hostName]) }
    }
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn: scan()
        case .unauthorized: error("Allow Bluetooth for Quota Meter Bluetooth in System Settings > Privacy & Security > Bluetooth")
        case .poweredOff: error("Mac Bluetooth is turned off")
        case .unsupported: error("Bluetooth LE is not supported")
        default: break
        }
    }
    func centralManager(_ central: CBCentralManager, didDiscover found: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard peripheral == nil, Date() >= retryAt else { return }
        if let pinnedID, found.identifier != pinnedID { return }
        let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? found.name ?? ""
        guard pinnedID != nil || name == "Sweetmeter" else { return }
        peripheral = found; found.delegate = self; phase = "connect"; lastAction = Date()
        central.stopScan(); central.connect(found)
    }
    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        phase = "discover"; lastAction = Date(); peripheral.discoverServices([serviceID])
    }
    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        self.peripheral = nil; ready = false; phase = "offline"; retryAt = Date().addingTimeInterval(5)
        emit(["event": "disconnected"])
    }
    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        self.peripheral = nil; control = nil; payload = nil; status = nil; ready = false
        frame = nil; writes.removeAll(); phase = "offline"; lastAction = Date()
        if retryAt < Date() { retryAt = Date().addingTimeInterval(2) }
        emit(["event": "disconnected"])
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard error == nil, let service = peripheral.services?.first(where: { $0.uuid == serviceID }) else {
            self.error("Quota meter BLE service is missing"); return
        }
        peripheral.discoverCharacteristics([controlID, dataID, statusID], for: service)
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard error == nil else { self.error("Could not discover BLE characteristics"); return }
        for characteristic in service.characteristics ?? [] {
            if characteristic.uuid == controlID { control = characteristic }
            if characteristic.uuid == dataID { payload = characteristic }
            if characteristic.uuid == statusID { status = characteristic }
        }
        guard let status, control != nil, payload != nil else { self.error("Incomplete BLE service"); return }
        peripheral.readValue(for: status)
    }
    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil, let bytes = characteristic.value else {
            self.error("BLE read failed: \(error?.localizedDescription ?? "empty response")"); return
        }
        lastAction = Date()
        if characteristic.uuid == statusID {
            guard let state = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                  state["protocol"] as? Int == 3 else { self.error("Unsupported meter protocol"); return }
            if let selected = state["selected_host"] as? String, !selected.isEmpty, selected != hostID {
                self.error("Meter selected another computer; hold its bottom button to change", retry: 30); return
            }
            emit(["event": "status", "device_id": peripheral.identifier.uuidString, "status": state])
            statusAt = Date()
            if ready { syncTime(); return }
            phase = "subscribe"; peripheral.setNotifyValue(true, for: control!)
        } else if characteristic.uuid == controlID {
            let message = [UInt8](bytes)
            if message == [82] { emit(["event": "refresh"]); return }
            if message.count == 2 && message[0] == 72 {
                if message[1] != 0 { self.error("Computer is not selected on the meter", retry: 30); return }
                ready = true; emit(["event": "connected", "device_id": peripheral.identifier.uuidString]); syncTime(); return
            }
            guard message.count == 10, message[0] == 65, frame != nil,
                  read32(message, 2) == sequence else { return }
            guard message[1] <= 1, read32(message, 6) == checksum else {
                self.error("Device rejected BLE frame (code \(message[1]))"); return
            }
            emit(["event": "ack", "ack": message[1] == 1 ? "SAME" : "FULL", "sequence": sequence,
                  "crc32": String(format: "%08x", checksum)])
            frame = nil; phase = "idle"; sendPending()
        }
    }
    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil, characteristic.isNotifying else { self.error("BLE notifications unavailable"); return }
        phase = "hello"
        write(Data([72]) + Data(hostID.utf8) + Data(hostName.prefix(20).utf8), for: control!, type: .withResponse)
    }
    func syncTime() {
        guard peripheral != nil, let control else { return }
        phase = "time"; lastAction = Date()
        let now = Date()
        let epoch = UInt32(now.timeIntervalSince1970)
        let zone = UInt32(bitPattern: Int32(TimeZone.current.secondsFromGMT(for: now)))
        write(Data([84] + little32(epoch) + little32(zone)), for: control, type: .withResponse)
    }
    func accept(_ object: [String: Any]) {
        guard let encoded = object["frame"] as? String, let bytes = Data(base64Encoded: encoded), bytes.count == 4000 else { return }
        queuedFrame = bytes; sendPending()
    }
    func sendPending() {
        guard ready, phase == "idle", let pending = queuedFrame, peripheral != nil, let control else { return }
        queuedFrame = nil; frame = pending; checksum = crc32(pending); offset = 0
        sequence &+= 1; phase = "begin"; lastAction = Date()
        write(Data([66] + little32(sequence) + little32(checksum) + [0xa0, 0x0f]), for: control, type: .withResponse)
    }
    func write(_ data: Data, for characteristic: CBCharacteristic, type: CBCharacteristicWriteType) {
        writes.append(phase); peripheral?.writeValue(data, for: characteristic, type: type)
    }
    func writeNext() {
        guard let peripheral, let frame, let payload, let control else { return }
        if offset == frame.count {
            phase = "commit"
            write(Data([67] + little32(sequence)), for: control, type: .withResponse); return
        }
        let count = min(frame.count - offset, min(180, peripheral.maximumWriteValueLength(for: .withResponse) - 2))
        guard count > 0 else { self.error("BLE MTU is too small"); return }
        let packet = Data([UInt8(truncatingIfNeeded: offset), UInt8(truncatingIfNeeded: offset >> 8)]) + frame.subdata(in: offset..<(offset + count))
        offset += count; phase = "data"; write(packet, for: payload, type: .withResponse)
    }
    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil else { self.error("BLE write failed: \(error!.localizedDescription)"); return }
        lastAction = Date()
        // Notifications may precede the write-complete callback; only advance the matching characteristic.
        let kind = writes.isEmpty ? "" : writes.removeFirst()
        if kind == "begin" || kind == "data" { writeNext() }
        else if kind == "time" && characteristic.uuid == controlID { phase = "idle"; sendPending() }
    }
}
let args = CommandLine.arguments
func argument(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), args.count > i + 1 else { return nil }; return args[i + 1]
}
guard let hostID = argument("--host"), UUID(uuidString: hostID) != nil else {
    emit(["event": "error", "error": "Missing stable companion identity"]); exit(1)
}
let bridge = Bridge(hostID: hostID.lowercased(), hostName: argument("--name") ?? "Quota Mac",
                    pinnedID: argument("--device").flatMap { UUID(uuidString: $0) })
DispatchQueue.global().async {
    while let line = readLine() {
        if let data = line.data(using: .utf8), let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            DispatchQueue.main.async { bridge.accept(object) }
        }
    }
    exit(0) // Companion parent exited: do not leave an orphan radio process.
}
RunLoop.main.run()
