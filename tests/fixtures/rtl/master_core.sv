// Sends one byte after reset and latches the reply, so the test can see every signal cross the top.
module master_core (
    input  logic       clk_i,
    input  logic       rst_ni,
    output logic       valid_o,
    output logic [7:0] wdata_o,
    input  logic [7:0] rdata_i,
    input  logic       ready_i,
    input  logic       err_i,
    output logic       debug_o,
    input  logic       irq_i,
    output logic [7:0] result_o
);
    assign wdata_o = 8'h40;
    assign debug_o = 1'b0;

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
            valid_o  <= 1'b0;
            result_o <= 8'h00;
        end else begin
            valid_o <= 1'b1;
            // result only moves when the reply, the tie-off, the default and the irq all arrive
            if (valid_o && ready_i && !err_i && irq_i) result_o <= rdata_i;
        end
    end
endmodule
