{
  description = "A flake for endscopetool";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    git-hooks.url = "github:cachix/git-hooks.nix";
    git-hooks.inputs.nixpkgs.follows = "nixpkgs";
    flint.url = "github:notashelf/flint";
    flint.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      git-hooks,
      flint,
      ...
    }:
    let
      forEachSystem = nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed;
      pkgsFor = system: nixpkgs.legacyPackages.${system};
      deps = ps: [
        (ps.opencv4.override { enableGtk3 = true; })
        ps.numpy
        ps.pillow
        ps.trio
      ];
    in
    {
      packages = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python313;
        in
        {
          default = python.pkgs.buildPythonApplication {
            pname = "endscopetool";
            version = "0.1.0";
            pyproject = true;
            src = ./.;
            nativeBuildInputs = [
              python.pkgs.setuptools
            ];
            dependencies = deps python.pkgs;
            buildInputs = [ pkgs.gtk3 ];
          };
        }
      );

      apps = forEachSystem (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/endscope";
        };
      });

      checks = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python313;
          python-with-mypy = python.withPackages (
            ps:
            (deps ps)
            ++ [
              ps.mypy
              ps.types-pillow
            ]
          );
        in
        {
          pre-commit-check = git-hooks.lib.${system}.run {
            src = ./.;
            hooks = {
              nixfmt-rfc-style.enable = true;
              mypy = {
                enable = true;
                settings = {
                  binPath = "${python-with-mypy}/bin/mypy";
                };
              };
              ruff.enable = true;
              ruff-format.enable = true;
              yamlfmt.enable = true;
              flint = {
                enable = true;
                name = "flint";
                entry = "${flint.packages.${system}.default}/bin/flint --fail-if-multiple-versions";
                files = "flake\\.(nix|lock)$";
              };
            };
          };
        }
      );

      formatter = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
          config = self.checks.${system}.pre-commit-check.config;
          script = ''
            ${pkgs.lib.getExe config.package} run --all-files --config ${config.configFile}
          '';
        in
        pkgs.writeShellScriptBin "pre-commit-run" script
      );

      devShells = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python313;
          pre-commit-check = self.checks.${system}.pre-commit-check;
        in
        {
          default = pkgs.mkShell {
            inherit (pre-commit-check) shellHook;
            buildInputs = pre-commit-check.enabledPackages;
            packages = [
              pkgs.nixfmt-rfc-style
              pkgs.gtk3
              flint.packages.${system}.default
              (python.withPackages deps)
            ];
          };
        }
      );
    };
}
