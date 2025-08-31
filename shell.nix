let
  pkgs = import <nixpkgs> { };
in
pkgs.mkShell {
  packages = [
    pkgs.nixfmt-rfc-style
    pkgs.gtk2
    (pkgs.python313.withPackages (ps: [
      (ps.opencv4.override { enableGtk2 = true; })
      ps.numpy
      ps.pillow
    ]))
  ];
}
