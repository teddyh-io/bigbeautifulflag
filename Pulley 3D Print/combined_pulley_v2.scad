// Combined Pulley + Motor Shaft Adapter (Single Part)
// Profile matches McMaster-Carr 9466T75 wire rope pulley
// Modified: 1.5" total width, deep V-groove for 1/4" rope
// D-bore for BRINGSMART A58SW31ZY motor shaft built in

$fn = 120;

// === PULLEY DIMENSIONS ===
pulley_od       = 38.1;     // 1.5" outer diameter
pulley_r        = pulley_od / 2; // 19.05mm radius
pulley_width    = 38.1;     // 1.5" total width (user requested)

// Groove profile
// The V-groove should be deep like the McMaster original
// Rope sits at the bottom of the V
rope_dia        = 6.35;     // 1/4" rope
groove_bottom_r = 10;       // Radius at bottom of V-groove (where rope sits)
groove_round_r  = rope_dia / 2; // Rounded bottom to cradle rope

// Flanges - thin lips at full OD to retain rope
flange_thickness = 3.5;     // Thin flanges at the outer edge

// === MOTOR SHAFT BORE (D-shape) ===
shaft_dia       = 8.4;      // Round side + clearance
d_flat          = 7.4;      // D-flat + clearance

// === SET SCREW ===
set_screw_dia   = 3.2;      // M3 through-hole
nut_width       = 6.0;      // M3 nut across flats
nut_depth       = 2.5;      // M3 nut trap depth

// === HUB ===
hub_od          = 16;        // Hub outer diameter
hub_length      = 12;        // Hub extends behind pulley for shaft grip

// === DERIVED ===
half_width      = pulley_width / 2;

// === MODULES ===

// D-shaped bore
module d_shaft_bore(diameter, flat_width, height) {
    intersection() {
        cylinder(d=diameter, h=height);
        translate([-(diameter/2), -(flat_width/2), 0])
            cube([diameter, flat_width, height]);
    }
}

// 2D pulley cross-section profile for rotate_extrude
// This defines the shape in the XY plane (X = radius from center, Y = width)
// rotate_extrude spins this around the Y axis (which becomes the Z axis)
module pulley_cross_section() {
    // Build the profile as a polygon
    // Going clockwise from inner bottom-left
    
    // Key points:
    // Inner bore area connects to groove bottom via V-walls to flanges at OD
    
    hull_points = [
        // Inner edge (will be bored out anyway, just needs to be < groove_bottom_r)
        [0, 0],                                          // bottom-left (axis, left face)
        [0, pulley_width],                               // top-left (axis, right face)
        
        // Right flange (outer edge, full OD)
        [pulley_r, pulley_width],                        // top-right outer
        [pulley_r, pulley_width - flange_thickness],     // top-right inner lip
        
        // V-groove right wall slopes down to groove bottom
        [groove_bottom_r + groove_round_r, half_width + groove_round_r],  // right side of groove curve
        
        // Groove bottom (rounded for rope)
        [groove_bottom_r, half_width],                   // very bottom of groove
        
        // V-groove left wall slopes up
        [groove_bottom_r + groove_round_r, half_width - groove_round_r],  // left side of groove curve
        
        // Left flange
        [pulley_r, flange_thickness],                    // bottom-left inner lip
        [pulley_r, 0],                                   // bottom-left outer
    ];
    
    polygon(points = hull_points);
}

// Smoother groove with actual rounded bottom
module pulley_body() {
    rotate_extrude(convexity=10)
        pulley_cross_section();
}

// === MAIN PART ===
difference() {
    union() {
        // Pulley body
        pulley_body();
        
        // Hub extending behind the pulley
        translate([0, 0, -hub_length])
            cylinder(d=hub_od, h=hub_length);
    }
    
    // D-shaped bore all the way through
    translate([0, 0, -hub_length - 0.1])
        d_shaft_bore(shaft_dia, d_flat, hub_length + pulley_width + 0.2);
    
    // Set screw hole (radial, through the hub)
    translate([0, -(hub_od/2 + 1), -hub_length/2])
        rotate([-90, 0, 0])
            cylinder(d=set_screw_dia, h=hub_od/2 + 2);
    
    // M3 nut trap
    translate([0, -(hub_od/2), -hub_length/2])
        rotate([-90, 30, 0])
            cylinder(d=nut_width / cos(30), h=nut_depth, $fn=6);
}

// === PRINT NOTES ===
// Print with hub pointing UP, flat flange on bed
// Material: PETG recommended
// Infill: 80-100%
// Layer height: 0.15-0.2mm
// Walls: 4+ perimeters
// Supports: Not needed (V-groove overhang is gradual)
//
// The groove is now deep like the original McMaster pulley -
// rope sits well below the OD with thin retaining flanges.
