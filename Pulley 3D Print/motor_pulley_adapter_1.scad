// Motor-to-Pulley Stepped Adapter
// Bottom: 8mm D-shaped bore (7mm flat) fits over motor shaft
// Top: 11.25mm stub inserts into pulley bore (measured 11.35mm)
// Set screw hole to lock onto motor shaft

// === PARAMETERS (adjust to tune fit) ===

// Motor shaft dimensions (measured: 7mm flat-to-flat)
shaft_dia = 8.4;          // ~8mm round side + 0.4mm clearance for 3D print
d_flat = 7.4;             // 7mm D-flat + 0.4mm clearance (was too tight at 6.9mm)
shaft_depth = 10;         // How deep the shaft inserts into adapter (mm)

// Pulley stub dimensions (measured: 11.35mm bore)
stub_dia = 11.25;         // Slightly under 11.35mm for snug press fit
stub_length = 14;         // How far stub inserts into pulley bore

// Collar (main body) dimensions
collar_dia = 18;          // Outer diameter of the collar section (wider than 11.25mm stub)
collar_height = shaft_depth; // Same as shaft bore depth

// Set screw
set_screw_dia = 3.2;      // M3 set screw hole (tap or use self-tapping)
set_screw_nut_dia = 6.0;  // M3 nut trap width (across flats)
set_screw_nut_depth = 2.5; // M3 nut trap depth

// === RESOLUTION ===
$fn = 80;

// === MODULES ===

// D-shaped bore (8mm round with 7mm flat)
module d_shaft_bore(diameter, flat_width, height) {
    intersection() {
        cylinder(d=diameter, h=height);
        translate([-(diameter/2), -(flat_width/2), 0])
            cube([diameter, flat_width, height]);
    }
}

// === MAIN ASSEMBLY ===
difference() {
    union() {
        // Bottom collar section
        cylinder(d=collar_dia, h=collar_height);
        
        // Top stub that inserts into pulley
        translate([0, 0, collar_height])
            cylinder(d=stub_dia, h=stub_length);
    }
    
    // D-shaped bore from bottom (motor shaft hole)
    translate([0, 0, -0.1])
        d_shaft_bore(shaft_dia, d_flat, shaft_depth + 0.2);
    
    // Set screw hole through collar wall (radial, hits the D-flat)
    // Positioned at mid-height of collar, pointing toward the flat
    translate([0, -(collar_dia/2 + 1), collar_height/2])
        rotate([-90, 0, 0])
            cylinder(d=set_screw_dia, h=collar_dia/2 + 2);
    
    // M3 nut trap on outside of set screw hole
    translate([0, -(collar_dia/2), collar_height/2])
        rotate([-90, 30, 0])
            cylinder(d=set_screw_nut_dia / cos(30), h=set_screw_nut_depth, $fn=6);
}

// === PRINT NOTES ===
// Print with stub pointing UP
// Material: PETG or PLA+ recommended for strength
// Layer height: 0.15-0.2mm for good bore accuracy
// Infill: 80-100%
// No supports needed
// 
// Assembly:
// 1. Slide D-bore onto motor shaft (may need light sanding)
// 2. Insert M3 set screw through nut trap to lock onto shaft
// 3. Press pulley onto the stub
//
// If fit is too loose/tight, adjust clearance values above
// and reprint. D-shaft tolerance is the most critical dimension.
